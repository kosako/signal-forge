import pytest

from app.analysis import llm


@pytest.fixture(autouse=True)
def _no_real_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    # テストから本物の codex(PATH の上の `codex`)で推論を始めない。朝バッチの段のように
    # config の [llm].engine からエンジンを作る経路は、偽のエンジンを渡し忘れると本物の LLM を
    # 呼び、時間と枠を使い、外部へ本文を送る(GH#349 の 1 本目で調査段の候補が増えて表に出た)。
    # 偽の実行ファイルを codex_bin に渡す CodexCliEngine のテストはそのまま動く
    original = llm.CodexCliEngine.complete_structured

    def guarded(self, *args, **kwargs):
        if self._codex_bin == "codex":
            raise AssertionError(
                "テストから本物の codex を起動しない(偽のエンジンを渡すか codex_bin を指定する)"
            )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(llm.CodexCliEngine, "complete_structured", guarded)
