# Tree sort: build a BST from inputs, then in-order traverse for sorted output.
def tree_sort(xs):
    def insert(n, v):  # insert v into BST rooted at n (None = empty)
        if n is None: return {'v': v, 'l': None, 'r': None}
        if v < n['v']: n['l'] = insert(n['l'], v)   # smaller -> left
        else: n['r'] = insert(n['r'], v)            # greater/equal -> right
        return n
    def walk(n): return [] if n is None else walk(n['l']) + [n['v']] + walk(n['r'])  # in-order
    root = None
    for x in xs: root = insert(root, x)  # build tree
    return walk(root)                    # flatten sorted
