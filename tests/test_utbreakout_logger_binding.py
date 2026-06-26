import ast
from pathlib import Path


def test_emas_does_not_reference_unbound_log_alias():
    source = Path("emas.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    unbound_log_reads = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "log"
        and isinstance(node.ctx, ast.Load)
    ]

    assert unbound_log_reads == []
