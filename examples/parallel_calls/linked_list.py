class Node:
    def __init__(self, value, next=None):
        self.value, self.next = value, next

head = Node(1, Node(2, Node(3)))
while head: print(head.value); head = head.next
