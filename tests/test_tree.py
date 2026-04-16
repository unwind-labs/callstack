"""Tests for TreeNode, ExecutionTree, and tree traversal helpers."""

from callstack import (
    TreeNode,
    ExecutionTree,
    _all_nodes,
    _find_node_by_session,
    _find_yielded_leaf,
    _node_depth,
    _find_parent_node,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(*root_nodes) -> ExecutionTree:
    """Build an ExecutionTree with the given root nodes."""
    return ExecutionTree(
        root_session_id="root-session",
        root_session_file="/tmp/root.jsonl",
        call_depth_base=1,
        nodes=list(root_nodes),
    )


def _leaf(node_id="leaf", **kwargs) -> TreeNode:
    return TreeNode(id=node_id, task=f"task-{node_id}", **kwargs)


# ---------------------------------------------------------------------------
# TreeNode serialization
# ---------------------------------------------------------------------------

class TestTreeNodeSerialization:

    def test_round_trip_leaf(self):
        node = _leaf("n1", status="complete", result="done")
        restored = TreeNode.from_dict(node.to_dict())
        assert restored.id == "n1"
        assert restored.status == "complete"
        assert restored.result == "done"
        assert restored.children == []

    def test_round_trip_with_children(self):
        child = _leaf("c1", status="complete", result="child-done")
        parent = TreeNode(
            id="p1", task="parent-task", status="running",
            children=[child],
        )
        restored = TreeNode.from_dict(parent.to_dict())
        assert restored.id == "p1"
        assert len(restored.children) == 1
        assert restored.children[0].id == "c1"
        assert restored.children[0].result == "child-done"

    def test_round_trip_preserves_all_fields(self):
        node = TreeNode(
            id="x", task="t", session_id="sess", clone_path="/tmp/c.jsonl",
            parent_lines=42, status="yielded", result=None,
            yield_question="what?", yield_source="x",
            error=None, duration=3.14, children=[],
        )
        d = node.to_dict()
        restored = TreeNode.from_dict(d)
        assert restored.session_id == "sess"
        assert restored.clone_path == "/tmp/c.jsonl"
        assert restored.parent_lines == 42
        assert restored.yield_question == "what?"
        assert restored.yield_source == "x"
        assert restored.duration == 3.14

    def test_round_trip_preserves_suggested_next(self):
        node = TreeNode(
            id="sn", task="task", status="complete",
            result="done", suggested_next="Run tests next",
        )
        restored = TreeNode.from_dict(node.to_dict())
        assert restored.suggested_next == "Run tests next"

    def test_suggested_next_defaults_to_none(self):
        minimal = {"id": "m", "task": "t"}
        node = TreeNode.from_dict(minimal)
        assert node.suggested_next is None

    def test_round_trip_preserves_summary(self):
        node = TreeNode(
            id="sm", task="task", status="complete",
            result="done", summary="Touched auth.py; chose JWT",
        )
        restored = TreeNode.from_dict(node.to_dict())
        assert restored.summary == "Touched auth.py; chose JWT"

    def test_summary_defaults_to_none(self):
        minimal = {"id": "m", "task": "t"}
        node = TreeNode.from_dict(minimal)
        assert node.summary is None

    def test_defaults_for_missing_keys(self):
        """from_dict should handle minimal dicts (only id + task required in practice)."""
        minimal = {"id": "m", "task": "t"}
        node = TreeNode.from_dict(minimal)
        assert node.status == "pending"
        assert node.parent_lines == 0
        assert node.duration == 0.0
        assert node.children == []


# ---------------------------------------------------------------------------
# ExecutionTree serialization
# ---------------------------------------------------------------------------

class TestExecutionTreeSerialization:

    def test_round_trip_empty(self):
        tree = _make_tree()
        restored = ExecutionTree.from_dict(tree.to_dict())
        assert restored.root_session_id == "root-session"
        assert restored.call_depth_base == 1
        assert restored.nodes == []

    def test_round_trip_with_nodes(self):
        tree = _make_tree(_leaf("a"), _leaf("b"))
        restored = ExecutionTree.from_dict(tree.to_dict())
        assert len(restored.nodes) == 2
        assert restored.nodes[0].id == "a"
        assert restored.nodes[1].id == "b"


# ---------------------------------------------------------------------------
# _all_nodes
# ---------------------------------------------------------------------------

class TestAllNodes:

    def test_flat(self):
        tree = _make_tree(_leaf("a"), _leaf("b"), _leaf("c"))
        assert [n.id for n in _all_nodes(tree)] == ["a", "b", "c"]

    def test_nested(self):
        """
        Tree:  a -> a1 -> a1a
               b
        """
        a1a = _leaf("a1a")
        a1 = TreeNode(id="a1", task="t", children=[a1a])
        a = TreeNode(id="a", task="t", children=[a1])
        b = _leaf("b")
        tree = _make_tree(a, b)
        ids = [n.id for n in _all_nodes(tree)]
        # Breadth-first: a, b, a1, a1a
        assert ids == ["a", "b", "a1", "a1a"]

    def test_empty(self):
        tree = _make_tree()
        assert _all_nodes(tree) == []


# ---------------------------------------------------------------------------
# _find_node_by_session
# ---------------------------------------------------------------------------

class TestFindNodeBySession:

    def test_found(self):
        node = _leaf("n1", session_id="sess-123")
        tree = _make_tree(node)
        assert _find_node_by_session(tree, "sess-123") is node

    def test_found_nested(self):
        child = _leaf("c", session_id="deep-sess")
        parent = TreeNode(id="p", task="t", children=[child])
        tree = _make_tree(parent)
        assert _find_node_by_session(tree, "deep-sess") is child

    def test_not_found(self):
        tree = _make_tree(_leaf("n", session_id="other"))
        assert _find_node_by_session(tree, "nonexistent") is None


# ---------------------------------------------------------------------------
# _find_yielded_leaf
# ---------------------------------------------------------------------------

class TestFindYieldedLeaf:

    def test_self_yield(self):
        node = _leaf("n", status="yielded", yield_source="n", yield_question="q?")
        assert _find_yielded_leaf(node) is node

    def test_child_yield(self):
        child = _leaf("c", status="yielded", yield_source="c", yield_question="child q?")
        parent = TreeNode(
            id="p", task="t", status="yielded",
            yield_source="c", children=[child],
        )
        assert _find_yielded_leaf(parent) is child

    def test_deep_chain(self):
        grandchild = _leaf("gc", status="yielded", yield_source="gc", yield_question="gc q?")
        child = TreeNode(
            id="c", task="t", status="yielded",
            yield_source="gc", children=[grandchild],
        )
        parent = TreeNode(
            id="p", task="t", status="yielded",
            yield_source="c", children=[child],
        )
        assert _find_yielded_leaf(parent) is grandchild

    def test_no_yield_source(self):
        node = _leaf("n", status="yielded", yield_source=None)
        assert _find_yielded_leaf(node) is node


# ---------------------------------------------------------------------------
# _node_depth
# ---------------------------------------------------------------------------

class TestNodeDepth:

    def test_root_depth(self):
        node = _leaf("n")
        tree = _make_tree(node)
        assert _node_depth(node, tree) == 0

    def test_child_depth(self):
        child = _leaf("c")
        parent = TreeNode(id="p", task="t", children=[child])
        tree = _make_tree(parent)
        assert _node_depth(parent, tree) == 0
        assert _node_depth(child, tree) == 1

    def test_grandchild_depth(self):
        gc = _leaf("gc")
        child = TreeNode(id="c", task="t", children=[gc])
        parent = TreeNode(id="p", task="t", children=[child])
        tree = _make_tree(parent)
        assert _node_depth(gc, tree) == 2


# ---------------------------------------------------------------------------
# _find_parent_node
# ---------------------------------------------------------------------------

class TestFindParentNode:

    def test_root_has_no_parent(self):
        node = _leaf("n")
        tree = _make_tree(node)
        assert _find_parent_node(node, tree) is None

    def test_child_parent(self):
        child = _leaf("c")
        parent = TreeNode(id="p", task="t", children=[child])
        tree = _make_tree(parent)
        assert _find_parent_node(child, tree) is parent

    def test_grandchild_parent(self):
        gc = _leaf("gc")
        child = TreeNode(id="c", task="t", children=[gc])
        parent = TreeNode(id="p", task="t", children=[child])
        tree = _make_tree(parent)
        assert _find_parent_node(gc, tree) is child
        assert _find_parent_node(child, tree) is parent
