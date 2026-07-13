from pathlib import Path

EMAS = Path("emas.py")
TEST = Path("tests/test_qh_flow_integration.py")

text = EMAS.read_text(encoding="utf-8")

old_actions = '''    "adaptive",
    "dual",
    "dualt",
    "dual_status",
    "rsp",
    "rspt",
    "rsp_status",
    "set",'''
new_actions = '''    "adaptive",
    "dual",
    "dualt",
    "dual_status",
    "qh",
    "qhflow",
    "qh_status",
    "rsp",
    "rspt",
    "rsp_status",
    "triple",
    "triplet",
    "triple_status",
    "set",'''

if old_actions not in text:
    raise SystemExit("callback action block not found")

text = text.replace(old_actions, new_actions, 1)
EMAS.write_text(text, encoding="utf-8")

test_text = TEST.read_text(encoding="utf-8")
test_name = "test_qh_and_triple_callback_actions_are_registered"
if test_name not in test_text:
    test_text += '''\n\ndef test_qh_and_triple_callback_actions_are_registered():
    emas = _emas_module()
    assert {
        "qh",
        "qhflow",
        "qh_status",
        "triple",
        "triplet",
        "triple_status",
    } <= emas.UTBREAKOUT_CALLBACK_ACTIONS
'''
    TEST.write_text(test_text, encoding="utf-8")
