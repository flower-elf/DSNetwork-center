"""检查文档与源码是否遵守 project-rule.md 的中文写作规范，违规时列出文件、行号与原文。

用 python tests/_test_doc_style.py 运行。同步上游后必须重跑：上游带回的写法会破坏这些规则。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 按本仓库规范书写的中文文档；docs/CHANGELOG.md 是上游的历史记录，不在其中。
DOCS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "project-rule.md",
    os.path.join("docs", "contributing-agents.md"),
    os.path.join("wrt", "README.md"),
)

CJK = "\u4e00-\u9fff"
FULL_PUNCT = re.escape("，。；：、？！（）“”‘’《》「」")

PROSE_RULES = (
    ("中文与英文、数字之间缺少空格",
     re.compile(r"[%s][A-Za-z0-9]|[A-Za-z0-9%%][%s]" % (CJK, CJK))),
    ("中文与行内代码之间缺少空格", re.compile(r"[%s]`x`|`x`[%s]" % (CJK, CJK))),
    ("中文句子使用了半角标点", re.compile(r"[%s][,;:?!(]|\)[%s]" % (CJK, CJK))),
    ("全角标点前后多了空格", re.compile(r" [%s]|[%s] " % (FULL_PUNCT, FULL_PUNCT))),
    ("中文省略号应写作 ⋯⋯", re.compile(r"[%s]\.\.\.|\.\.\.[%s]|…" % (CJK, CJK))),
)

# 文档与源码共用的禁用词：（说明，正则）。
WORD_RULES = (
    ("“不是⋯⋯而是”句式", re.compile(r"不是[^。\n]{0,40}而是")),
    ("写作“登录”", re.compile(r"登陆")),
    ("写作“账号”", re.compile(r"帐号")),
    ("称呼读者用“你”", re.compile(r"您")),
    ("列举不用“等等”结尾", re.compile(r"等等")),
    ("黑话", re.compile(r"钉死|钉住|钉在|落地")),
    ("台湾用语，改用大陆常用词",
     re.compile(r"身分|回传|预设|讯息|宣告|(?<!菜单)栏位|呼叫|搜寻|字串|网路|串流|记忆体"
                r"|使用者|物件|回圈|阵列|例外|自動|手動|切換")),
)

# 只查文档：CSS 注释里的“左对齐”“与标题对齐”是排版术语。
DOC_ONLY_RULES = (
    ("黑话，改用“一致”", re.compile(r"(?<![左右])对齐")),
)

SOURCE_DIRS = (os.path.join(ROOT, "app"),)
SOURCE_EXTS = (".py", ".js", ".css", ".html")
SKIP_DIRS = {"__pycache__", "vendor"}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def prose_lines(text):
    """文档里的正文行，跳过围栏代码块。"""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def normalise(line):
    """去掉 HTML 标签、链接地址、行首列表与引用标记、表格分隔，行内代码换成占位符。"""
    line = re.sub(r"`[^`]*`", "`x`", line)
    line = re.sub(r"<[^>]+>", " ", line)
    line = re.sub(r"\]\([^)]*\)", "]", line)
    line = re.sub(r"https?://\S+", " ", line)
    line = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line)
    line = re.sub(r"^\s*>\s*", "", line)
    return re.sub(r"\s*\|\s*", "|", line)


def source_files():
    for base in SOURCE_DIRS:
        for folder, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if name.endswith(SOURCE_EXTS):
                    yield os.path.join(folder, name)


class DocStyleTests(unittest.TestCase):
    maxDiff = None

    def test_docs_follow_the_writing_rules(self):
        offenders = []
        for rel in DOCS:
            # 根 README.md 暂时撤除；其他声明文档缺失属于仓库缺陷。
            if not os.path.isfile(os.path.join(ROOT, rel)):
                if rel == "README.md":
                    continue
                offenders.append("%s: 文件不存在" % rel)
                continue
            # project-rule.md 要列出禁用词本身，只检查空格与标点。
            rules = PROSE_RULES
            if rel != "project-rule.md":
                rules = rules + WORD_RULES + DOC_ONLY_RULES
            for number, raw in prose_lines(read(rel)):
                line = normalise(raw)
                for label, pattern in rules:
                    if pattern.search(line):
                        offenders.append("%s:%d %s：%s" % (rel, number, label, raw.strip()))
        self.assertEqual(offenders, [], "\n" + "\n".join(offenders))

    def test_doc_file_names_are_lowercase_ascii(self):
        allowed = {"README.md", "README-old.md", "AGENTS.md", "CONTRIBUTING.md",
                   "CHANGELOG.md"}
        offenders = []
        for folder in (ROOT, os.path.join(ROOT, "docs")):
            for name in os.listdir(folder):
                if not name.endswith(".md") or name in allowed:
                    continue
                if not re.fullmatch(r"[a-z0-9-]+\.md", name):
                    offenders.append(os.path.relpath(os.path.join(folder, name), ROOT))
        self.assertEqual(offenders, [])

    def test_source_text_avoids_banned_words(self):
        offenders = []
        for path in source_files():
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    for label, pattern in WORD_RULES:
                        if pattern.search(line):
                            offenders.append("%s:%d %s：%s" % (
                                os.path.relpath(path, ROOT), number, label, line.strip()))
        self.assertEqual(offenders, [], "\n" + "\n".join(offenders))

    def test_readme_carries_no_changelog(self):
        """README 只描述当前功能，版本记录写在 GitHub Release 说明里。"""
        if not os.path.isfile(os.path.join(ROOT, "README.md")):
            self.skipTest("README.md 暂时撤除")
        text = read("README.md")
        self.assertNotRegex(text, r"(?m)^#{1,3} .*(版本更新记录|更新日志|Unreleased|Changelog)")
        self.assertNotRegex(text, r"(?m)^#{2,3} v\d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
