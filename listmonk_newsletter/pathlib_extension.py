from pathlib import Path


def append_text(self, text: str):
    with self.open(mode="a", encoding="utf-8") as file:
        file.write(text)


# Monkey patch the PosixPath class
# PosixPath.append_text = append_text
Path.append_text = append_text
