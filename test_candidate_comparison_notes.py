"""
「なぜこの商品が1位か」(build_candidate_comparison_notes、プレミアム機能
「AI比較」)の個別化のテスト。

- normalize_candidate()が商品固有データ(active_ingredients等)を欠落させずに
  保持することの確認(finalize_step_data経由の統合テスト)
- build_candidate_comparison_notes()が商品ごとに実質的に異なる文章を生成する
  こと
- 成分データが無い商品でもクラッシュせず妥当な文章になること
- 商品データに存在しない成分名を理由に使わない(捏造しない)こと
- 実際のスコア関係と説明内容が矛盾しないこと

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。

実行方法:
    DATABASE_URL=postgresql://localhost/rumilog_test python3 -m pytest test_candidate_comparison_notes.py -v
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402  (DATABASE_URL設定後にimportする必要がある)


def _candidate(
    name,
    brand="テストブランド",
    score=80,
    base_score=80,
    improve_score=0,
    routine_score=0,
    price_ref=2000,
    active_ingredients=None,
    support_ingredients=None,
    main_functions=None,
    concerns=None,
    source="db",
    candidate_score_reasons=None,
):
    """normalize_candidate()通過後の形(必要フィールドのみ)を模したdict。
    candidate_score_reasonsは採点根拠トレース(score_product()等の副産物)を
    模したリスト。[{"axis","rule","label","matched_product_feature",
    "matched_user_condition","points"}, ...]。"""
    return {
        "brand": brand,
        "name": name,
        "score": score,
        "base_score": base_score,
        "improve_score": improve_score,
        "routine_score": routine_score,
        "source": source,
        "price_ref": price_ref,
        "active_ingredients": active_ingredients or [],
        "support_ingredients": support_ingredients or [],
        "main_functions": main_functions or [],
        "skin_types": [],
        "concerns": concerns or [],
        "texture": "",
        "formulation": [],
        "candidate_score_reasons": candidate_score_reasons or [],
    }


def _reason(rule, label, feature="", condition="", points=10, axis="base"):
    """テスト用candidate_score_reasonsエントリを作る小さなヘルパー。"""
    return {
        "axis": axis,
        "rule": rule,
        "label": label,
        "matched_product_feature": feature,
        "matched_user_condition": condition,
        "points": points,
    }


class BuildCandidateComparisonNotesTests(unittest.TestCase):
    # why_bestは1位単独の属性ではなく、2位・3位との実比較(比較対象に無い
    # 特徴)から理由を組み立てること
    def test_why_best_cites_feature_absent_from_comparison_candidates(self):
        candidates = [
            _candidate(
                "モイストローション", base_score=90,
                candidate_score_reasons=[
                    _reason("common_main_function_purpose_match", "商品の機能が今回の目的と一致する",
                            feature="高保湿ケア", condition="乾燥対策", points=6),
                ],
            ),
            _candidate("競合A", base_score=60, candidate_score_reasons=[]),
        ]
        step = {"category": "化粧水", "purpose": "乾燥対策"}
        user_data = {"oil": "dry", "concerns": ["dryness"]}

        result = app.build_candidate_comparison_notes(candidates, step, user_data)
        self.assertIn("高保湿ケア", result["why_best"])
        self.assertIn("モイストローション", result["why_best"])

    # 全候補が共通して持つ特徴を「1位の決め手」として扱わないこと
    def test_shared_feature_across_all_candidates_is_not_cited_as_reason(self):
        candidates = [
            _candidate("商品A", main_functions=["高保湿ケア"], active_ingredients=["niacinamide"], base_score=80),
            _candidate("商品B", main_functions=["高保湿ケア"], active_ingredients=["niacinamide"], base_score=80),
            _candidate("商品C", main_functions=["高保湿ケア"], active_ingredients=["niacinamide"], base_score=80),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("高保湿ケア", result["why_best"])
        self.assertNotIn("ナイアシンアミド", result["why_best"])
        # 構造的な差もスコア差(同点)も無いため、根拠の無い断定はしない
        self.assertIn("明確な優位点は確認できません", result["why_best"])

    # スコアが完全に同点の場合、「上回った」等の優位表現を使わないこと
    def test_tied_scores_are_not_described_as_superior(self):
        candidates = [
            _candidate("商品A", base_score=80, improve_score=10, routine_score=5),
            _candidate("商品B", base_score=80, improve_score=10, routine_score=5),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("上回っ", result["why_best"])
        self.assertNotIn("優位だった", result["why_best"])
        self.assertIn("明確な優位点は確認できません", result["why_best"])

    # 1位と2位・3位で実際にスコア差がある軸のみを理由に使い、
    # 単純に1位自身の3スコアの最大値(この例ではroutine=5がbase=10等より小さく
    # 最大にならない)を機械的に選ばないこと。improve軸で他候補より本当に
    # 優位な場合にimprove適合スコアが理由として使われることを確認する。
    def test_score_axis_reason_reflects_actual_gap_not_own_max_value(self):
        candidates = [
            _candidate("改善重視商品", base_score=10, improve_score=90, routine_score=5),
            _candidate("競合A", base_score=10, improve_score=20, routine_score=5),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertIn("改善適合スコア", result["why_best"])
        self.assertNotIn("基本適合スコア", result["why_best"])

    # ランキング評価に使われていない属性(main_functions/active_ingredients/
    # support_ingredients/concerns以外、例: textureのような未対応フィールド)は
    # 1位だけが持つ値でも理由に使わないこと
    def test_non_ranked_field_is_never_used_as_reason(self):
        candidates = [
            _candidate("商品A", base_score=80),
            _candidate("商品B", base_score=80),
        ]
        candidates[0]["texture"] = "とろみのある珍しいテクスチャー"
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("とろみのある珍しいテクスチャー", result["why_best"])

    # 異なる商品データを渡せば、why_bestが実質的に異なる文章になること
    # (同一テンプレートへの機械的な値埋め込みではないことの確認)
    def test_why_best_differs_for_different_products(self):
        step = {"category": "美容液", "purpose": ""}
        user_data = {"oil": "oily", "concerns": []}

        result_a = app.build_candidate_comparison_notes(
            [
                _candidate("商品A", base_score=90, candidate_score_reasons=[
                    _reason("common_main_function_purpose_match", "商品の機能が今回の目的と一致する",
                            feature="毛穴引き締め", points=6),
                ]),
                _candidate("競合1", base_score=60, candidate_score_reasons=[]),
            ],
            step, user_data,
        )
        result_b = app.build_candidate_comparison_notes(
            [
                _candidate("商品B", improve_score=90, base_score=10, candidate_score_reasons=[]),
                _candidate("競合2", improve_score=20, base_score=10, candidate_score_reasons=[]),
            ],
            step, user_data,
        )
        self.assertNotEqual(result_a["why_best"], result_b["why_best"])
        # 文の骨格自体が異なること(片方は採点根拠(reasons)由来、もう片方はスコア軸由来)
        self.assertIn("毛穴引き締め", result_a["why_best"])
        self.assertIn("改善適合スコア", result_b["why_best"])

    # 比較根拠の種類(構造的差/スコアのみ/根拠なし)ごとに文の骨格自体が変わること
    # (語尾や単語だけを変えた見せかけの個別化になっていないことの確認)
    def test_different_reason_kinds_produce_structurally_different_sentences(self):
        structural = app.build_candidate_comparison_notes(
            [
                _candidate("商品A", base_score=80, candidate_score_reasons=[
                    _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む",
                            feature="ナイアシンアミド", points=25),
                ]),
                _candidate("競合A", base_score=80, candidate_score_reasons=[]),
            ],
            {}, {},
        )
        score_only = app.build_candidate_comparison_notes(
            [
                _candidate("商品B", base_score=80),
                _candidate("競合B", base_score=50),
            ],
            {}, {},
        )
        no_evidence = app.build_candidate_comparison_notes(
            [
                _candidate("商品C", base_score=80),
                _candidate("競合C", base_score=80),
            ],
            {}, {},
        )
        self.assertIn("ナイアシンアミド", structural["why_best"])
        self.assertNotIn("ナイアシンアミド", score_only["why_best"])
        self.assertIn("基本適合スコア", score_only["why_best"])
        self.assertIn("明確な優位点は確認できません", no_evidence["why_best"])
        # 3者とも文構造が異なること
        self.assertNotEqual(structural["why_best"], score_only["why_best"])
        self.assertNotEqual(score_only["why_best"], no_evidence["why_best"])

    # 成分・機能データが無い商品でもクラッシュせず、価格・スコアのみで説明すること
    def test_why_best_falls_back_to_price_and_score_when_no_ingredient_data(self):
        candidates = [_candidate("シンプル乳液", main_functions=[], active_ingredients=[], price_ref=1500, base_score=70)]
        result = app.build_candidate_comparison_notes(candidates, {"category": "乳液", "purpose": ""}, {})
        self.assertNotEqual(result["why_best"], "")
        self.assertNotIn("None", result["why_best"])

    # 比較対象(2位・3位)が存在しない場合は優位性を一切主張しないこと
    def test_no_comparison_candidates_does_not_claim_superiority(self):
        candidates = [_candidate("単独商品", base_score=80, main_functions=["何か"])]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("上回っ", result["why_best"])
        self.assertNotIn("優位", result["why_best"])
        self.assertIn("唯一の選択肢", result["why_best"])

    # 空リストでもクラッシュしない
    def test_empty_candidates_returns_empty_notes(self):
        result = app.build_candidate_comparison_notes([], {}, {})
        self.assertEqual(result, {"why_best": "", "diffs": []})

    # 全スコアが0の候補ではwhy_bestを空にする(根拠のない断定をしない)
    def test_all_zero_scores_produces_empty_why_best(self):
        candidates = [_candidate("スコア無し商品", base_score=0, improve_score=0, routine_score=0, main_functions=["何か"])]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertEqual(result["why_best"], "")

    # 商品データに存在しない成分名を理由に使わない(捏造しない)こと
    def test_does_not_mention_ingredients_absent_from_product_data(self):
        candidates = [
            _candidate("無成分商品", active_ingredients=[], support_ingredients=[], main_functions=[], base_score=80),
            _candidate("競合", active_ingredients=[], support_ingredients=[], main_functions=[], base_score=50),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        for fake_ingredient_label in ["レチノール", "ビタミンC", "ナイアシンアミド"]:
            self.assertNotIn(fake_ingredient_label, result["why_best"])

    # 根拠が弱い(構造差もスコア差も無い)場合、存在しない差を作らず
    # 中立的な文言になること
    def test_weak_evidence_falls_back_to_neutral_text_without_fabrication(self):
        candidates = [
            _candidate("商品A", base_score=80, improve_score=0, routine_score=0),
            _candidate("商品B", base_score=80, improve_score=0, routine_score=0),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertEqual(result["why_best"], "比較した候補との間に明確な優位点は確認できませんでした。総合スコアの僅差で選ばれています。")

    # diffs: 2位・3位それぞれの説明が、商品固有データにより実質的に異なること
    def test_diffs_are_distinct_for_different_runner_up_products(self):
        candidates = [
            _candidate("1位商品", score=90, base_score=90, active_ingredients=["niacinamide"], price_ref=3000),
            _candidate("2位商品", score=70, base_score=70, active_ingredients=["retinol"], price_ref=2000),
            _candidate("3位商品", score=60, base_score=60, active_ingredients=[], price_ref=5000),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertEqual(len(result["diffs"]), 2)
        self.assertNotEqual(result["diffs"][0]["text"], result["diffs"][1]["text"])
        # 1位のみが持つ成分(niacinamide→ナイアシンアミド)が2位との差分として言及される
        self.assertIn("ナイアシンアミド", result["diffs"][0]["text"])

    # 価格が高いのに「優れている」と断定しない(中立的な事実表現であること)
    def test_price_difference_phrasing_does_not_overclaim_superiority(self):
        candidates = [
            _candidate("高価格1位商品", score=90, base_score=90, price_ref=5000, active_ingredients=[]),
            _candidate("安価2位商品", score=60, base_score=60, price_ref=1000, active_ingredients=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        text = result["diffs"][0]["text"]
        self.assertIn("高いが総合スコアでは上回る", text)
        self.assertNotIn("優れている", text)
        self.assertNotIn("優位", text)

    # 実際のスコア関係と説明内容が矛盾しない: improve_scoreで他候補より
    # 実際に優位な場合のみ「改善適合スコア」に言及すること
    # (test_score_axis_reason_reflects_actual_gap_not_own_max_valueで詳細確認)
    def test_dominant_score_component_matches_actual_scores(self):
        candidates = [
            _candidate("改善重視商品", base_score=10, improve_score=90, routine_score=5),
            _candidate("競合", base_score=10, improve_score=20, routine_score=5),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertIn("改善適合スコア", result["why_best"])
        self.assertNotIn("基本適合スコア", result["why_best"])


class BuildCandidateComparisonTableDiffIntegrationTests(unittest.TestCase):
    """
    独立した「次点候補」セクションを廃止し、build_candidate_comparison_notes()の
    diffsを商品比較テーブル(build_candidate_comparison_table)へ統合したことの
    テスト。
    """

    def test_table_keeps_rank_name_price_score_cost_perf_and_best_value(self):
        candidates = [
            _candidate("1位商品", score=90, base_score=90, price_ref=3000),
            _candidate("2位商品", score=70, base_score=70, price_ref=1000),
            _candidate("3位商品", score=60, base_score=60, price_ref=5000),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        for row in rows:
            self.assertIn("name", row)
            self.assertIn("price", row)
            self.assertIn("score", row)
            self.assertIn("cost_perf", row)
            self.assertIn("is_best_value", row)
        # 2位商品(価格1000, score70)が最もコスパが良い
        self.assertTrue(rows[1]["is_best_value"])

    def test_existing_diffs_are_not_lost_when_merged_into_table(self):
        candidates = [
            _candidate("1位商品", score=90, base_score=90, active_ingredients=["niacinamide"], price_ref=3000),
            _candidate("2位商品", score=70, base_score=70, active_ingredients=["retinol"], price_ref=2000),
            _candidate("3位商品", score=60, base_score=60, active_ingredients=[], price_ref=5000),
        ]
        notes = app.build_candidate_comparison_notes(candidates, {}, {})
        rows = app.build_candidate_comparison_table(candidates, notes["diffs"])

        rank2_row = next(r for r in rows if r["rank"] == 2)
        rank3_row = next(r for r in rows if r["rank"] == 3)
        # diffs(次点候補が持っていた情報)がそのままdiff_from_bestへ残っていること
        self.assertEqual(rank2_row["diff_from_best"], notes["diffs"][0]["text"])
        self.assertEqual(rank3_row["diff_from_best"], notes["diffs"][1]["text"])
        self.assertNotEqual(rank2_row["diff_from_best"], "")

    def test_rank_1_has_no_diff_from_best(self):
        """1位には「1位との違い」は不要(空文字のまま)。"""
        candidates = [
            _candidate("1位商品", score=90, base_score=90, price_ref=3000),
            _candidate("2位商品", score=70, base_score=70, price_ref=2000),
        ]
        notes = app.build_candidate_comparison_notes(candidates, {}, {})
        rows = app.build_candidate_comparison_table(candidates, notes["diffs"])
        rank1_row = next(r for r in rows if r["rank"] == 1)
        self.assertEqual(rank1_row["diff_from_best"], "")

    def test_table_without_diffs_argument_still_works(self):
        """diffsを渡さない呼び出しでも従来通り動作すること(後方互換)。"""
        candidates = [
            _candidate("1位商品", score=90, base_score=90, price_ref=3000),
            _candidate("2位商品", score=70, base_score=70, price_ref=2000),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertTrue(all(r["diff_from_best"] == "" for r in rows))


class NormalizeCandidateFieldRetentionTests(unittest.TestCase):
    """
    normalize_candidate()(finalize_step_data内のクロージャ)が商品固有データを
    落とさずに保持することを、公開関数finalize_step_data経由で確認する。
    """

    def test_finalize_step_data_preserves_score_reasons_into_candidate_comparison(self):
        step = {
            "category": "美容液",
            "purpose": "",
            "product": "ナイアシンアミド美容液",
            "brand": "テストブランド",
            "top_candidates": [
                {
                    "brand": "テストブランド",
                    "name": "ナイアシンアミド美容液",
                    "score": 90,
                    "base_score": 90,
                    "improve_score": 0,
                    "routine_score": 0,
                    "price_ref": 2500,
                    "source": "db",
                    "_base_reasons": [
                        _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む",
                                feature="ナイアシンアミド", points=25),
                    ],
                },
                {
                    "brand": "競合ブランド",
                    "name": "競合美容液",
                    "score": 60,
                    "base_score": 60,
                    "improve_score": 0,
                    "routine_score": 0,
                    "price_ref": 2000,
                    "source": "db",
                    "_base_reasons": [],
                },
            ],
        }
        result_step = app.finalize_step_data(dict(step), {"oil": "oily", "concerns": []})
        why_best = result_step.get("candidate_comparison", {}).get("why_best", "")
        # _base_reasons(score_product()の副産物)がnormalize_candidate通過後も
        # candidate_score_reasonsとして保持されていなければ、この特徴語は
        # why_bestに一切現れないはず。比較対象(競合美容液)は持たないため、
        # 実際の差分として採用されるはず。
        self.assertIn("ナイアシンアミド", why_best, f"採点根拠がwhy_bestへ反映されていません: {why_best!r}")

    def test_finalize_step_data_passes_diffs_into_candidate_comparison_table(self):
        """
        回帰テスト: finalize_step_data()の通常経路(楽天フォールバックの
        _refresh_candidate_comparison_after_swap()とは別)でも、
        build_candidate_comparison_table()へdiffsが渡り、2位・3位の行に
        diff_from_bestが入ること。

        9bdcbde/e3ce516のいずれのコミットでも、finalize_step_data内の
        この呼び出し箇所だけdiffsを渡すよう更新されておらず、商品比較表の
        「1位との違い」列が本番では常に空になっていた(hunk分割時の
        マーカー不一致により、複数ラウンドにわたり未コミットのまま
        作業ツリーにだけ残っていた不具合)。今回のセッションで発見・修正した。
        """
        step = {
            "category": "美容液",
            "purpose": "",
            "product": "1位美容液",
            "brand": "ブランドA",
            "top_candidates": [
                {"brand": "ブランドA", "name": "1位美容液", "score": 90, "base_score": 90,
                 "improve_score": 0, "routine_score": 0, "price_ref": 3000, "source": "db"},
                {"brand": "ブランドB", "name": "2位美容液", "score": 70, "base_score": 70,
                 "improve_score": 0, "routine_score": 0, "price_ref": 2000, "source": "db"},
            ],
        }
        result_step = app.finalize_step_data(dict(step), {"oil": "oily", "concerns": []})
        table = result_step.get("candidate_comparison_table", [])
        diffs = result_step.get("candidate_comparison", {}).get("diffs", [])
        self.assertTrue(diffs, "diffsが生成されていません(テスト前提が崩れています)")
        rank2_row = next((r for r in table if r["rank"] == 2), None)
        self.assertIsNotNone(rank2_row)
        self.assertNotEqual(rank2_row.get("diff_from_best", ""), "")
        self.assertEqual(rank2_row["diff_from_best"], diffs[0]["text"])


class RakutenFallbackCandidateComparisonRefreshTests(unittest.TestCase):
    """
    楽天フォールバックで1位商品(top_candidates[0])が差し替わった後、
    _refresh_candidate_comparison_after_swap()がcandidate_comparison/
    candidate_comparison_tableを最新のtop_candidatesで再計算し、
    表示中の商品(step.product/top_candidates[0])とwhy_best/商品比較表の
    1位が一致することの確認。
    """

    def test_refresh_recomputes_why_best_to_match_new_top_candidate(self):
        old_top = [
            _candidate("旧1位商品", base_score=90, candidate_score_reasons=[
                _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む",
                        feature="レチノール", points=25),
                _reason("common_availability", "日本での入手性が確認されている",
                        feature="amazon", points=5),
            ]),
            _candidate("新1位商品", base_score=90, candidate_score_reasons=[
                _reason("ingredient_focus_active_match", "今回重視する成分を主成分として含む",
                        feature="ナイアシンアミド", points=25),
                _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている",
                        feature="sensitive_ok=yes", points=12),
            ]),
        ]
        step = {"category": "美容液", "purpose": "", "top_candidates": old_top}
        # フォールバック前: 古いtop_candidatesを基準にcandidate_comparisonが
        # finalize_result_data()で確定済み、という状態を再現する。
        step["candidate_comparison"] = app.build_candidate_comparison_notes(old_top, step, {})
        step["candidate_comparison_table"] = app.build_candidate_comparison_table(
            old_top, step["candidate_comparison"]["diffs"]
        )
        self.assertIn("旧1位商品", step["candidate_comparison"]["why_best"])

        # _try_rakuten_fallback_candidate()が実際に行うのと同じ並び替え
        # (楽天リンク取得成功candidateを先頭に繰り上げ)を再現する。
        new_winner = old_top[1]
        step["product"] = new_winner["name"]
        step["brand"] = new_winner["brand"]
        step["top_candidates"] = [new_winner] + [c for c in old_top if c is not new_winner]

        app._refresh_candidate_comparison_after_swap(step, {})

        self.assertIn(step["product"], step["candidate_comparison"]["why_best"])
        self.assertNotIn("旧1位商品", step["candidate_comparison"]["why_best"])
        self.assertEqual(step["top_candidates"][0]["name"], step["product"])
        self.assertEqual(step["candidate_comparison_table"][0]["name"], step["product"])
        self.assertEqual(step["candidate_comparison_table"][0]["brand"], step["brand"])

    # 4位以下(=既存の商品比較表・次点候補の対象=上位3件の外)からフォールバック
    # して新1位になったケースでも、古い上位3商品の比較情報が残らないこと
    def test_refresh_handles_fallback_from_beyond_top_three(self):
        pool = [
            _candidate("1位", base_score=90),
            _candidate("2位", base_score=85),
            _candidate("3位", base_score=80),
            _candidate("4位からの繰り上げ", base_score=50, candidate_score_reasons=[
                _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている", feature="sensitive_ok=yes", points=12),
            ]),
        ]
        step = {"category": "美容液", "purpose": "", "top_candidates": pool}
        step["candidate_comparison"] = app.build_candidate_comparison_notes(pool, step, {})
        step["candidate_comparison_table"] = app.build_candidate_comparison_table(
            pool, step["candidate_comparison"]["diffs"]
        )
        self.assertEqual(
            [r["name"] for r in step["candidate_comparison_table"]],
            ["1位", "2位", "3位"],
        )

        fourth = pool[3]
        step["product"] = fourth["name"]
        step["brand"] = fourth["brand"]
        step["top_candidates"] = [fourth] + [c for c in pool if c is not fourth]

        app._refresh_candidate_comparison_after_swap(step, {})

        table_names = [r["name"] for r in step["candidate_comparison_table"]]
        self.assertEqual(table_names[0], "4位からの繰り上げ")
        self.assertIn(fourth["name"], step["candidate_comparison"]["why_best"])

    # top_candidatesを持たないstep(美容機器・サプリメント)は何もしない
    def test_refresh_noop_for_step_without_top_candidates(self):
        step = {"category": "美容機器", "product": "RF美顔器"}
        app._refresh_candidate_comparison_after_swap(step, {})
        self.assertNotIn("candidate_comparison", step)


if __name__ == "__main__":
    unittest.main()
