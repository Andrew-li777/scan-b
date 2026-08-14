from src.translate import needs_translation
from src.models import SubtitleEntry


def _e(text: str) -> SubtitleEntry:
    return SubtitleEntry(start=0.0, end=1.0, text=text)


class TestNeedsTranslation:
    def test_empty_list(self):
        assert needs_translation([]) is False

    def test_all_chinese(self):
        assert needs_translation([_e("你好世界")]) is False

    def test_all_english(self):
        assert needs_translation([_e("hello world")]) is True

    def test_majority_chinese(self):
        # 4 CJK out of 9 non-space chars = 44% > 30%
        assert needs_translation([_e("你好世界 world")]) is False

    def test_minority_chinese(self):
        # 1 CJK out of 5 non-space chars = 20% < 30%
        assert needs_translation([_e("hello world 你")]) is True

    def test_whitespace_only(self):
        assert needs_translation([_e("   ")]) is False

    def test_threshold_zero(self):
        # threshold=0 means never need translation
        assert needs_translation([_e("hello world")], threshold=0.0) is False

    def test_threshold_one_pure_chinese(self):
        # threshold=1.0: only pure CJK (ratio=1.0) doesn't need translation
        assert needs_translation([_e("你好世界")], threshold=1.0) is False

    def test_threshold_one_mixed(self):
        # threshold=1.0: any non-CJK char triggers translation
        assert needs_translation([_e("你好世界a")], threshold=1.0) is True

    def test_only_first_20_sampled(self):
        entries = [_e("hello")] * 20 + [_e("你好世界")]
        # First 20 are all english → needs translation
        assert needs_translation(entries) is True
