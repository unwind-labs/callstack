from __future__ import annotations

from typing import Optional


class Node:
    def __init__(self, value):
        self.value = value
        self.next: Optional[Node] = None


class LinkedList:
    def __init__(self):
        self.head: Optional[Node] = None

    def append(self, value):
        node = Node(value)
        if self.head is None:
            self.head = node
            return
        curr = self.head
        while curr.next is not None:
            curr = curr.next
        curr.next = node

    def reverse(self):
        prev = None
        curr = self.head
        while curr is not None:
            nxt = curr.next
            curr.next = prev
            prev = curr
            curr = nxt
        self.head = prev

    def __str__(self):
        values = []
        curr = self.head
        while curr is not None:
            values.append(str(curr.value))
            curr = curr.next
        return " -> ".join(values) if values else "(empty)"


if __name__ == "__main__":
    ll = LinkedList()
    for v in [1, 2, 3, 4, 5]:
        ll.append(v)
    print("Original:", ll)
    ll.reverse()
    print("Reversed:", ll)
