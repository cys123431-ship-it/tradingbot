from pathlib import Path

path = Path('emas.py')
text = path.read_text(encoding='utf-8')
text = text.replace("return '\n'.join([", "return '\\n'.join([")
text = text.replace("return '\n'.join(lines)", "return '\\n'.join(lines)")
path.write_text(text, encoding='utf-8')
