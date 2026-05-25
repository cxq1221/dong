"""edit 工具测试：覆盖唯一替换、歧义匹配和安全错误。"""
from dong.tools import execute


def _execute_edit(cwd, filepath, old_string, new_string):
    """调用 execute('edit', ...) 并返回 ToolResult 的测试辅助函数。"""
    return execute(
        "edit",
        {"filepath": filepath, "old_string": old_string, "new_string": new_string},
        cwd,
    )


class TestEditSuccess:
    """edit 工具的成功路径测试。"""

    def test_replace_single_occurrence(self, tmp_path):
        """文件中唯一出现的字符串应能被替换。"""
        f = tmp_path / "hello.txt"
        f.write_text("Hello, world!")
        result = _execute_edit(str(tmp_path), "hello.txt", "world", "there")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "Hello, there!"

    def test_replace_at_beginning(self, tmp_path):
        """文件开头的文本应能被替换。"""
        f = tmp_path / "begin.txt"
        f.write_text("start middle end")
        result = _execute_edit(str(tmp_path), "begin.txt", "start", "BEGIN")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "BEGIN middle end"

    def test_replace_at_end(self, tmp_path):
        """文件末尾的文本应能被替换。"""
        f = tmp_path / "end.txt"
        f.write_text("start middle end")
        result = _execute_edit(str(tmp_path), "end.txt", "end", "END")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "start middle END"

    def test_replace_with_empty_new_string(self, tmp_path):
        """用空字符串替换时应删除旧文本。"""
        f = tmp_path / "remove.txt"
        f.write_text("remove this word")
        result = _execute_edit(str(tmp_path), "remove.txt", " this", "")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "remove word"

    def test_replace_empty_old_string_with_new(self, tmp_path):
        """空 old_string 是边界情况，应被判定为歧义匹配。"""
        f = tmp_path / "insert.txt"
        f.write_text("hello")
        result = _execute_edit(str(tmp_path), "insert.txt", "", "prefix")
        # 空字符串存在于任意字符串的多个位置，content.count('') 会返回 len(content)+1。
        # 因此它必须走 Ambiguous 分支，避免插入到不确定位置。
        assert result.success is False
        assert "Ambiguous" in result.error

    def test_replace_unicode_content(self, tmp_path):
        """包含 Unicode 字符的替换应正常工作。"""
        f = tmp_path / "unicode.txt"
        f.write_text("héllo wörld")
        result = _execute_edit(str(tmp_path), "unicode.txt", "wörld", "värld")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "héllo värld"

    def test_replace_with_same_string(self, tmp_path):
        """把字符串替换成自身时应保持内容不变。"""
        f = tmp_path / "same.txt"
        f.write_text("unchanged content")
        result = _execute_edit(str(tmp_path), "same.txt", "unchanged", "unchanged")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "unchanged content"


class TestEditErrors:
    """edit 工具的错误路径测试。"""

    def test_old_string_not_found(self, tmp_path):
        """old_string 不存在时应返回失败。"""
        f = tmp_path / "notfound.txt"
        f.write_text("some content here")
        result = _execute_edit(str(tmp_path), "notfound.txt", "nonexistent", "replacement")
        assert result.success is False
        assert "not found" in result.error

    def test_old_string_ambiguous(self, tmp_path):
        """old_string 出现多次时应返回歧义错误。"""
        f = tmp_path / "ambiguous.txt"
        f.write_text("repeat repeat repeat")
        result = _execute_edit(str(tmp_path), "ambiguous.txt", "repeat", "once")
        assert result.success is False
        assert "Ambiguous" in result.error
        # 歧义替换失败后，文件内容必须保持不变。
        assert f.read_text(encoding="utf-8") == "repeat repeat repeat"

    def test_path_traversal_denied(self, tmp_path):
        """带 ../ 的逃逸路径应在编辑前被拒绝。"""
        result = _execute_edit(str(tmp_path), "../outside.txt", "old", "new")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_file_not_found(self, tmp_path):
        """编辑不存在的文件应返回失败。"""
        result = _execute_edit(str(tmp_path), "nonexistent.txt", "old", "new")
        assert result.success is False
        assert "not found" in result.error or "No such file" in result.error

    def test_invalid_input_type(self, tmp_path):
        """filepath 类型非法时应返回输入校验错误。"""
        result = execute(
            "edit",
            {"filepath": 123, "old_string": "old", "new_string": "new"},
            str(tmp_path),
        )
        assert result.success is False
        assert "Invalid input" in result.error
