from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Generic, Iterable, NewType, TypeVar, cast

import rich.repr
from rich.style import NULL_STYLE, Style
from rich.text import Text, TextType

from .. import events
from .._cache import LRUCache
from .._immutable_sequence_view import ImmutableSequenceView
from .._loop import loop_last
from .._segment_tools import line_pad
from .._types import MessageTarget
from ..binding import Binding, BindingType
from ..geometry import Region, Size, clamp
from ..message import Message
from ..reactive import reactive, var
from ..scroll_view import ScrollView
from ..strip import Strip

if TYPE_CHECKING:
    from typing_extensions import TypeAlias

NodeID = NewType("NodeID", int)
TreeDataType = TypeVar("TreeDataType")
EventTreeDataType = TypeVar("EventTreeDataType")

LineCacheKey: TypeAlias = "tuple[int | tuple, ...]"

TOGGLE_STYLE = Style.from_meta({"toggle": True})


@dataclass
class _TreeLine(Generic[TreeDataType]):
    path: list[TreeNode[TreeDataType]]
    last: bool

    @property
    def node(self) -> TreeNode[TreeDataType]:
        """TreeNode: The node associated with this line."""
        return self.path[-1]

    def _get_guide_width(self, guide_depth: int, show_root: bool) -> int:
        """Get the cell width of the line as rendered.

        Args:
            guide_depth: The guide depth (cells in the indentation).

        Returns:
            Width in cells.
        """
        if show_root:
            return 2 + (max(0, len(self.path) - 1)) * guide_depth
        else:
            guides = 2
            if len(self.path) > 1:
                guides += (len(self.path) - 1) * guide_depth

        return guides


class TreeNodes(ImmutableSequenceView["TreeNode[TreeDataType]"]):
    """An immutable collection of `TreeNode`."""


@rich.repr.auto
class TreeNode(Generic[TreeDataType]):
    """An object that represents a "node" in a tree control."""

    def __init__(
        self,
        tree: Tree[TreeDataType],
        parent: TreeNode[TreeDataType] | None,
        id: NodeID,
        label: Text,
        data: TreeDataType | None = None,
        *,
        expanded: bool = True,
        allow_expand: bool = True,
    ) -> None:
        self._tree = tree
        self._parent = parent
        self._id = id
        self._label = tree.process_label(label)
        self.data = data
        self._expanded = expanded
        self._children: list[TreeNode[TreeDataType]] = []

        self._hover_ = False
        self._selected_ = False
        self._allow_expand = allow_expand
        self._updates: int = 0
        self._line: int = -1

    def __rich_repr__(self) -> rich.repr.Result:
        yield self._label.plain
        yield self.data

    def _reset(self) -> None:
        self._hover_ = False
        self._selected_ = False
        self._updates += 1

    @property
    def children(self) -> TreeNodes[TreeDataType]:
        """TreeNodes[TreeDataType]: The child nodes of a TreeNode."""
        return TreeNodes(self._children)

    @property
    def line(self) -> int:
        """int: Get the line number for this node, or -1 if it is not displayed."""
        return self._line

    @property
    def _hover(self) -> bool:
        """bool: Check if the mouse is over the node."""
        return self._hover_

    @_hover.setter
    def _hover(self, hover: bool) -> None:
        self._updates += 1
        self._hover_ = hover

    @property
    def _selected(self) -> bool:
        """bool: Check if the node is selected."""
        return self._selected_

    @_selected.setter
    def _selected(self, selected: bool) -> None:
        self._updates += 1
        self._selected_ = selected

    @property
    def id(self) -> NodeID:
        """NodeID: Get the node ID."""
        return self._id

    @property
    def parent(self) -> TreeNode[TreeDataType] | None:
        """TreeNode[TreeDataType] | None: The parent of the node."""
        return self._parent

    @property
    def is_expanded(self) -> bool:
        """bool: Check if the node is expanded."""
        return self._expanded

    @property
    def is_last(self) -> bool:
        """bool: Check if this is the last child."""
        if self._parent is None:
            return True
        return bool(
            self._parent._children and self._parent._children[-1] == self,
        )

    @property
    def allow_expand(self) -> bool:
        """bool: Check if the node is allowed to expand."""
        return self._allow_expand

    @allow_expand.setter
    def allow_expand(self, allow_expand: bool) -> None:
        self._allow_expand = allow_expand
        self._updates += 1

    def _expand(self, expand_all: bool) -> None:
        """Mark the node as expanded (its children are shown).

        Args:
            expand_all: If `True` expand all offspring at all depths.
        """
        self._expanded = True
        self._updates += 1
        if expand_all:
            for child in self.children:
                child._expand(expand_all)

    def expand(self) -> None:
        """Expand the node (show its children)."""
        self._expand(False)
        self._tree._invalidate()

    def expand_all(self) -> None:
        """Expand the node (show its children) and all those below it."""
        self._expand(True)
        self._tree._invalidate()

    def _collapse(self, collapse_all: bool) -> None:
        """Mark the node as collapsed (its children are hidden).

        Args:
            collapse_all: If `True` collapse all offspring at all depths.
        """
        self._expanded = False
        self._updates += 1
        if collapse_all:
            for child in self.children:
                child._collapse(collapse_all)

    def collapse(self) -> None:
        """Collapse the node (hide its children)."""
        self._collapse(False)
        self._tree._invalidate()

    def collapse_all(self) -> None:
        """Collapse the node (hide its children) and all those below it."""
        self._collapse(True)
        self._tree._invalidate()

    def toggle(self) -> None:
        """Toggle the node's expanded state."""
        if self._expanded:
            self.collapse()
        else:
            self.expand()

    def toggle_all(self) -> None:
        """Toggle the node's expanded state and make all those below it match."""
        if self._expanded:
            self.collapse_all()
        else:
            self.expand_all()

    @property
    def label(self) -> TextType:
        """TextType: The label for the node."""
        return self._label

    @label.setter
    def label(self, new_label: TextType) -> None:
        self.set_label(new_label)

    def set_label(self, label: TextType) -> None:
        """Set a new label for the node.

        Args:
            label: A str or Text object with the new label.
        """
        self._updates += 1
        text_label = self._tree.process_label(label)
        self._label = text_label

    def add(
        self,
        label: TextType,
        data: TreeDataType | None = None,
        *,
        expand: bool = False,
        allow_expand: bool = True,
    ) -> TreeNode[TreeDataType]:
        """Add a node to the sub-tree.

        Args:
            label: The new node's label.
            data: Data associated with the new node.
            expand: Node should be expanded. Defaults to True.
            allow_expand: Allow use to expand the node via keyboard or mouse. Defaults to True.

        Returns:
            A new Tree node
        """
        text_label = self._tree.process_label(label)
        node = self._tree._add_node(self, text_label, data)
        node._expanded = expand
        node._allow_expand = allow_expand
        self._updates += 1
        self._children.append(node)
        self._tree._invalidate()
        return node

    def add_leaf(
        self, label: TextType, data: TreeDataType | None = None
    ) -> TreeNode[TreeDataType]:
        """Add a 'leaf' node (a node that can not expand).

        Args:
            label: Label for the node.
            data: Optional data. Defaults to None.

        Returns:
            New node.
        """
        node = self.add(label, data, expand=False, allow_expand=False)
        return node


class Tree(Generic[TreeDataType], ScrollView, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("space", "toggle_node", "Toggle", show=False),
        Binding("up", "cursor_up", "Cursor Up", show=False),
        Binding("down", "cursor_down", "Cursor Down", show=False),
    ]
    """
    | Key(s) | Description |
    | :- | :- |
    | enter | Select the current item. |
    | space | Toggle the expand/collapsed space of the current item. |
    | up | Move the cursor up. |
    | down | Move the cursor down. |
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "tree--label",
        "tree--guides",
        "tree--guides-hover",
        "tree--guides-selected",
        "tree--cursor",
        "tree--highlight",
        "tree--highlight-line",
    }
    """
    | Class | Description |
    | :- | :- |
    | `tree--cursor` | Targets the cursor. |
    | `tree--guides` | Targets the indentation guides. |
    | `tree--guides-hover` | Targets the indentation guides under the cursor. |
    | `tree--guides-selected` | Targets the indentation guides that are selected. |
    | `tree--highlight` | Targets the highlighted items. |
    | `tree--highlight-line` | Targets the lines under the cursor. |
    | `tree--label` | Targets the (text) labels of the items. |
    """

    DEFAULT_CSS = """
    Tree {
        background: $panel;
        color: $text;
    }
    Tree > .tree--label {

    }
    Tree > .tree--guides {
        color: $success-darken-3;
    }

    Tree > .tree--guides-hover {
        color: $success;
        text-style: bold;
    }

    Tree > .tree--guides-selected {
        color: $warning;
        text-style: bold;
    }

    Tree > .tree--cursor {
        background: $secondary-darken-2;
        color: $text;
        text-style: bold;
    }

    Tree:focus > .tree--cursor {
        background: $secondary;
    }

    Tree > .tree--highlight {
        text-style: underline;
    }

    Tree > .tree--highlight-line {
        background: $boost;
    }

    """

    show_root = reactive(True)
    """bool: Show the root of the tree."""
    hover_line = var(-1)
    """int: The line number under the mouse pointer, or -1 if not under the mouse pointer."""
    cursor_line = var(-1)
    """int: The line with the cursor, or -1 if no cursor."""
    show_guides = reactive(True)
    """bool: Enable display of tree guide lines."""
    guide_depth = reactive(4, init=False)
    """int: The indent depth of tree nodes."""
    auto_expand = var(True)
    """bool: Auto expand tree nodes when clicked."""

    LINES: dict[str, tuple[str, str, str, str]] = {
        "default": (
            "  ",
            "│ ",
            "└─",
            "├─",
        ),
        "bold": (
            "  ",
            "┃ ",
            "┗━",
            "┣━",
        ),
        "double": (
            "  ",
            "║ ",
            "╚═",
            "╠═",
        ),
    }

    class NodeCollapsed(Generic[EventTreeDataType], Message, bubble=True):
        """Event sent when a node is collapsed.

        Can be handled using `on_tree_node_collapsed` in a subclass of `Tree` or in a
        parent node in the DOM.

        Attributes:
            node: The node that was collapsed.
        """

        def __init__(
            self, sender: MessageTarget, node: TreeNode[EventTreeDataType]
        ) -> None:
            self.node: TreeNode[EventTreeDataType] = node
            super().__init__(sender)

    class NodeExpanded(Generic[EventTreeDataType], Message, bubble=True):
        """Event sent when a node is expanded.

        Can be handled using `on_tree_node_expanded` in a subclass of `Tree` or in a
        parent node in the DOM.

        Attributes:
            node: The node that was expanded.
        """

        def __init__(
            self, sender: MessageTarget, node: TreeNode[EventTreeDataType]
        ) -> None:
            self.node: TreeNode[EventTreeDataType] = node
            super().__init__(sender)

    class NodeHighlighted(Generic[EventTreeDataType], Message, bubble=True):
        """Event sent when a node is highlighted.

        Can be handled using `on_tree_node_highlighted` in a subclass of `Tree` or in a
        parent node in the DOM.

        Attributes:
            node: The node that was highlighted.
        """

        def __init__(
            self, sender: MessageTarget, node: TreeNode[EventTreeDataType]
        ) -> None:
            self.node: TreeNode[EventTreeDataType] = node
            super().__init__(sender)

    class NodeSelected(Generic[EventTreeDataType], Message, bubble=True):
        """Event sent when a node is selected.

        Can be handled using `on_tree_node_selected` in a subclass of `Tree` or in a
        parent node in the DOM.

        Attributes:
            node: The node that was selected.
        """

        def __init__(
            self, sender: MessageTarget, node: TreeNode[EventTreeDataType]
        ) -> None:
            self.node: TreeNode[EventTreeDataType] = node
            super().__init__(sender)

    def __init__(
        self,
        label: TextType,
        data: TreeDataType | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)

        text_label = self.process_label(label)

        self._updates = 0
        self._tree_nodes: dict[NodeID, TreeNode[TreeDataType]] = {}
        self._current_id = 0
        self.root = self._add_node(None, text_label, data)

        self._line_cache: LRUCache[LineCacheKey, Strip] = LRUCache(1024)
        self._tree_lines_cached: list[_TreeLine] | None = None
        self._cursor_node: TreeNode[TreeDataType] | None = None

    @property
    def cursor_node(self) -> TreeNode[TreeDataType] | None:
        """TreeNode | Node: The currently selected node, or ``None`` if no selection."""
        return self._cursor_node

    @property
    def last_line(self) -> int:
        """int: the index of the last line."""
        return len(self._tree_lines) - 1

    def process_label(self, label: TextType):
        """Process a str or Text in to a label. Maybe overridden in a subclass to change modify how labels are rendered.

        Args:
            label: Label.

        Returns:
            A Rich Text object.
        """
        if isinstance(label, str):
            text_label = Text.from_markup(label)
        else:
            text_label = label
        first_line = text_label.split()[0]
        return first_line

    def _add_node(
        self,
        parent: TreeNode[TreeDataType] | None,
        label: Text,
        data: TreeDataType | None,
        expand: bool = False,
    ) -> TreeNode[TreeDataType]:
        node = TreeNode(self, parent, self._new_id(), label, data, expanded=expand)
        self._tree_nodes[node._id] = node
        self._updates += 1
        return node

    def render_label(
        self, node: TreeNode[TreeDataType], base_style: Style, style: Style
    ) -> Text:
        """Render a label for the given node. Override this to modify how labels are rendered.

        Args:
            node: A tree node.
            base_style: The base style of the widget.
            style: The additional style for the label.

        Returns:
            A Rich Text object containing the label.
        """
        node_label = node._label.copy()
        node_label.stylize(style)

        if node._allow_expand:
            prefix = (
                "▼ " if node.is_expanded else "▶ ",
                base_style + TOGGLE_STYLE,
            )
        else:
            prefix = ("", base_style)

        text = Text.assemble(prefix, node_label)
        return text

    def get_label_width(self, node: TreeNode[TreeDataType]) -> int:
        """Get the width of the nodes label.

        The default behavior is to call `render_node` and return the cell length. This method may be
        overridden in a sub-class if it can be done more efficiently.

        Args:
            node: A node.

        Returns:
            Width in cells.
        """
        label = self.render_label(node, NULL_STYLE, NULL_STYLE)
        return label.cell_len

    def clear(self) -> None:
        """Clear all nodes under root."""
        self._line_cache.clear()
        self._tree_lines_cached = None
        self._current_id = 0
        root_label = self.root._label
        root_data = self.root.data
        self.root = TreeNode(
            self,
            None,
            self._new_id(),
            root_label,
            root_data,
            expanded=True,
        )
        self._updates += 1
        self.refresh()

    def reset(self, label: TextType, data: TreeDataType | None = None) -> None:
        """Clear the tree and reset the root node.

        Args:
            label: The label for the root node.
            data: Optional data for the root node.
        """
        self.clear()
        self.root.label = label
        self.root.data = data

    def select_node(self, node: TreeNode[TreeDataType] | None) -> None:
        """Move the cursor to the given node, or reset cursor.

        Args:
            node: A tree node, or None to reset cursor.
        """
        self.cursor_line = -1 if node is None else node._line

    def get_node_at_line(self, line_no: int) -> TreeNode[TreeDataType] | None:
        """Get the node for a given line.

        Args:
            line_no: A line number.

        Returns:
            A tree node, or ``None`` if there is no node at that line.
        """
        try:
            line = self._tree_lines[line_no]
        except IndexError:
            return None
        else:
            return line.node

    class UnknownNodeID(Exception):
        """Exception raised when referring to an unknown `TreeNode` ID."""

    def get_node_by_id(self, node_id: NodeID) -> TreeNode[TreeDataType]:
        """Get a tree node by its ID.

        Args:
            node_id: The ID of the node to get.

        Returns:
            The node associated with that ID.

        Raises:
            Tree.UnknownID: Raised if the `TreeNode` ID is unknown.
        """
        try:
            return self._tree_nodes[node_id]
        except KeyError:
            raise self.UnknownNodeID(f"Unknown NodeID ({node_id}) in tree") from None

    def validate_cursor_line(self, value: int) -> int:
        """Prevent cursor line from going outside of range."""
        return clamp(value, 0, len(self._tree_lines) - 1)

    def validate_guide_depth(self, value: int) -> int:
        """Restrict guide depth to reasonable range."""
        return clamp(value, 2, 10)

    def _invalidate(self) -> None:
        """Invalidate caches."""
        self._line_cache.clear()
        self._tree_lines_cached = None
        self._updates += 1
        self.root._reset()
        self.refresh(layout=True)

    def _on_mouse_move(self, event: events.MouseMove):
        meta = event.style.meta
        if meta and "line" in meta:
            self.hover_line = meta["line"]
        else:
            self.hover_line = -1

    def _new_id(self) -> NodeID:
        """Create a new node ID.

        Returns:
            A unique node ID.
        """
        id = self._current_id
        self._current_id += 1
        return NodeID(id)

    def _get_node(self, line: int) -> TreeNode[TreeDataType] | None:
        try:
            tree_line = self._tree_lines[line]
        except IndexError:
            return None
        else:
            return tree_line.node

    def _get_label_region(self, line: int) -> Region | None:
        """Returns the region occupied by the label of the node at line `line`.

        This can be used, e.g., when scrolling to that line such that the label
        is visible after the scroll.

        Args:
            line: A line number.

        Returns:
            The region occupied by the label, or `None` if the
            line is not in the tree.
        """
        try:
            tree_line = self._tree_lines[line]
        except IndexError:
            return None
        region_x = tree_line._get_guide_width(self.guide_depth, self.show_root)
        region_width = self.get_label_width(tree_line.node)
        return Region(region_x, line, region_width, 1)

    def watch_hover_line(self, previous_hover_line: int, hover_line: int) -> None:
        previous_node = self._get_node(previous_hover_line)
        if previous_node is not None:
            self._refresh_node(previous_node)
            previous_node._hover = False

        node = self._get_node(hover_line)
        if node is not None:
            self._refresh_node(node)
            node._hover = True

    def watch_cursor_line(self, previous_line: int, line: int) -> None:
        previous_node = self._get_node(previous_line)
        if previous_node is not None:
            self._refresh_node(previous_node)
            previous_node._selected = False
            self._cursor_node = None

        node = self._get_node(line)
        if node is not None:
            self._refresh_node(node)
            node._selected = True
            self._cursor_node = node
            if previous_node != node:
                self.post_message_no_wait(self.NodeHighlighted(self, node))

    def watch_guide_depth(self, guide_depth: int) -> None:
        self._invalidate()

    def watch_show_root(self, show_root: bool) -> None:
        self.cursor_line = -1
        self._invalidate()

    def scroll_to_line(self, line: int) -> None:
        """Scroll to the given line.

        Args:
            line: A line number.
        """
        region = self._get_label_region(line)
        if region is not None:
            self.scroll_to_region(region)

    def scroll_to_node(self, node: TreeNode[TreeDataType]) -> None:
        """Scroll to the given node.

        Args:
            node: Node to scroll in to view.
        """
        line = node._line
        if line != -1:
            self.scroll_to_line(line)

    def refresh_line(self, line: int) -> None:
        """Refresh (repaint) a given line in the tree.

        Args:
            line: Line number.
        """
        region = Region(0, line - self.scroll_offset.y, self.size.width, 1)
        self.refresh(region)

    def _refresh_node_line(self, line: int) -> None:
        node = self._get_node(line)
        if node is not None:
            self._refresh_node(node)

    def _refresh_node(self, node: TreeNode[TreeDataType]) -> None:
        """Refresh a node and all its children.

        Args:
            node: A tree node.
        """
        scroll_y = self.scroll_offset.y
        height = self.size.height
        visible_lines = self._tree_lines[scroll_y : scroll_y + height]
        for line_no, line in enumerate(visible_lines, scroll_y):
            if node in line.path:
                self.refresh_line(line_no)

    @property
    def _tree_lines(self) -> list[_TreeLine]:
        if self._tree_lines_cached is None:
            self._build()
        assert self._tree_lines_cached is not None
        return self._tree_lines_cached

    async def _on_idle(self, event: events.Idle) -> None:
        """Check tree needs a rebuild on idle."""
        # Property calls build if required
        self._tree_lines

    def _build(self) -> None:
        """Builds the tree by traversing nodes, and creating tree lines."""

        TreeLine = _TreeLine
        lines: list[_TreeLine] = []
        add_line = lines.append

        root = self.root

        def add_node(
            path: list[TreeNode[TreeDataType]], node: TreeNode[TreeDataType], last: bool
        ) -> None:
            child_path = [*path, node]
            node._line = len(lines)
            add_line(TreeLine(child_path, last))
            if node._expanded:
                for last, child in loop_last(node._children):
                    add_node(child_path, child, last)

        if self.show_root:
            add_node([], root, True)
        else:
            for node in self.root._children:
                add_node([], node, True)
        self._tree_lines_cached = lines

        guide_depth = self.guide_depth
        show_root = self.show_root
        get_label_width = self.get_label_width

        def get_line_width(line: _TreeLine) -> int:
            return get_label_width(line.node) + line._get_guide_width(
                guide_depth, show_root
            )

        if lines:
            width = max([get_line_width(line) for line in lines])
        else:
            width = self.size.width

        self.virtual_size = Size(width, len(lines))
        if self.cursor_line != -1:
            if self.cursor_node is not None:
                self.cursor_line = self.cursor_node._line
            if self.cursor_line >= len(lines):
                self.cursor_line = -1
        self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        scroll_x, scroll_y = self.scroll_offset
        style = self.rich_style
        return self._render_line(
            y + scroll_y,
            scroll_x,
            scroll_x + width,
            style,
        )

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        tree_lines = self._tree_lines
        width = self.size.width

        if y >= len(tree_lines):
            return Strip.blank(width, base_style)

        line = tree_lines[y]

        is_hover = self.hover_line >= 0 and any(node._hover for node in line.path)

        cache_key = (
            y,
            is_hover,
            width,
            self._updates,
            self.has_focus,
            tuple(node._updates for node in line.path),
        )
        if cache_key in self._line_cache:
            strip = self._line_cache[cache_key]
        else:
            base_guide_style = self.get_component_rich_style(
                "tree--guides", partial=True
            )
            guide_hover_style = base_guide_style + self.get_component_rich_style(
                "tree--guides-hover", partial=True
            )
            guide_selected_style = base_guide_style + self.get_component_rich_style(
                "tree--guides-selected", partial=True
            )

            hover = self.root._hover
            selected = self.root._selected and self.has_focus

            def get_guides(style: Style) -> tuple[str, str, str, str]:
                """Get the guide strings for a given style.

                Args:
                    style: A Style object.

                Returns:
                    Strings for space, vertical, terminator and cross.
                """
                lines: tuple[Iterable[str], Iterable[str], Iterable[str], Iterable[str]]
                if self.show_guides:
                    lines = self.LINES["default"]
                    if style.bold:
                        lines = self.LINES["bold"]
                    elif style.underline2:
                        lines = self.LINES["double"]
                else:
                    lines = ("  ", "  ", "  ", "  ")

                guide_depth = max(0, self.guide_depth - 2)
                guide_lines = tuple(
                    f"{characters[0]}{characters[1] * guide_depth} "
                    for characters in lines
                )
                return cast("tuple[str, str, str, str]", guide_lines)

            if is_hover:
                line_style = self.get_component_rich_style("tree--highlight-line")
            else:
                line_style = base_style

            guides = Text(style=line_style)
            guides_append = guides.append

            guide_style = base_guide_style
            for node in line.path[1:]:
                if hover:
                    guide_style = guide_hover_style
                if selected:
                    guide_style = guide_selected_style

                space, vertical, _, _ = get_guides(guide_style)
                guide = space if node.is_last else vertical
                if node != line.path[-1]:
                    guides_append(guide, style=guide_style)
                hover = hover or node._hover
                selected = (selected or node._selected) and self.has_focus

            if len(line.path) > 1:
                _, _, terminator, cross = get_guides(guide_style)
                if line.last:
                    guides.append(terminator, style=guide_style)
                else:
                    guides.append(cross, style=guide_style)

            label_style = self.get_component_rich_style("tree--label", partial=True)
            if self.hover_line == y:
                label_style += self.get_component_rich_style(
                    "tree--highlight", partial=True
                )
            if self.cursor_line == y:
                label_style += self.get_component_rich_style(
                    "tree--cursor", partial=False
                )

            label = self.render_label(line.path[-1], line_style, label_style).copy()
            label.stylize(Style(meta={"node": line.node._id, "line": y}))
            guides.append(label)

            segments = list(guides.render(self.app.console))
            pad_width = max(self.virtual_size.width, width)
            segments = line_pad(segments, 0, pad_width - guides.cell_len, line_style)
            strip = self._line_cache[cache_key] = Strip(segments)

        strip = strip.crop(x1, x2)
        return strip

    def _on_resize(self, event: events.Resize) -> None:
        self._line_cache.grow(event.size.height)
        self._invalidate()

    def _toggle_node(self, node: TreeNode[TreeDataType]) -> None:
        if not node.allow_expand:
            return
        if node.is_expanded:
            node.collapse()
            self.post_message_no_wait(self.NodeCollapsed(self, node))
        else:
            node.expand()
            self.post_message_no_wait(self.NodeExpanded(self, node))

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if "line" in meta:
            cursor_line = meta["line"]
            if meta.get("toggle", False):
                node = self.get_node_at_line(cursor_line)
                if node is not None:
                    self._toggle_node(node)

            else:
                self.cursor_line = cursor_line
                await self.action("select_cursor")

    def _on_styles_updated(self) -> None:
        self._invalidate()

    def action_cursor_up(self) -> None:
        """Move the cursor up one node."""
        if self.cursor_line == -1:
            self.cursor_line = self.last_line
        else:
            self.cursor_line -= 1
        self.scroll_to_line(self.cursor_line)

    def action_cursor_down(self) -> None:
        """Move the cursor down one node."""
        if self.cursor_line == -1:
            self.cursor_line = 0
        else:
            self.cursor_line += 1
        self.scroll_to_line(self.cursor_line)

    def action_page_down(self) -> None:
        """Move the cursor down a page's-worth of nodes."""
        if self.cursor_line == -1:
            self.cursor_line = 0
        self.cursor_line += self.scrollable_content_region.height - 1
        self.scroll_to_line(self.cursor_line)

    def action_page_up(self) -> None:
        """Move the cursor up a page's-worth of nodes."""
        if self.cursor_line == -1:
            self.cursor_line = self.last_line
        self.cursor_line -= self.scrollable_content_region.height - 1
        self.scroll_to_line(self.cursor_line)

    def action_scroll_home(self) -> None:
        """Move the cursor to the top of the tree."""
        self.cursor_line = 0
        self.scroll_to_line(self.cursor_line)

    def action_scroll_end(self) -> None:
        """Move the cursor to the bottom of the tree.

        Note:
            Here bottom means vertically, not branch depth.
        """
        self.cursor_line = self.last_line
        self.scroll_to_line(self.cursor_line)

    def action_toggle_node(self) -> None:
        """Toggle the expanded state of the target node."""
        try:
            line = self._tree_lines[self.cursor_line]
        except IndexError:
            pass
        else:
            self._toggle_node(line.path[-1])

    def action_select_cursor(self) -> None:
        """Cause a select event for the target node.

        Note:
            If `auto_expand` is `True` use of this action on a non-leaf node
            will cause both an expand/collapse event to occour, as well as a
            selected event.
        """
        try:
            line = self._tree_lines[self.cursor_line]
        except IndexError:
            pass
        else:
            node = line.path[-1]
            if self.auto_expand:
                self._toggle_node(node)
            self.post_message_no_wait(self.NodeSelected(self, node))
