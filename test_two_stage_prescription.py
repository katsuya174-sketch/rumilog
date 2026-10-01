"""
「2段階処方」(hydration/barrierともに50以下の場合、高刺激系統(A〜E)を
1種に制限し、それ以外は商品を削除せずdeferred_stepsへ保留する)のテスト。

設計(ユーザーとの合意事項):
- トリガー条件は既存プロンプト【商品選定時の刺激配慮】のhydration<=50 AND
  barrier<=50をそのまま使う(新しい閾値は発明しない)。
- B×C(レチノイド×AHA/BHA)は既存の_evaluate_retinoid_aha_bha_weekly_
  combination()(頻度調整・両方維持、テスト済み)が既に対応済みのため、
  本ロジックの対象から除外する(専用ルールが存在する組み合わせは専用
  ルールを優先する)。
- 残す1系統は、Phase1改善優先順位(premium_improvement_priority)→各系統の
  適合度→現在の刺激リスク(infer_active_profile)→同程度なら低刺激側、の
  順で決定する。
- 商品候補は削除せず、外れた分はdata["deferred_steps"]へ保持する。

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


def _retinoid_step(use_days=None):
    return {
        "product": "レチノール美容液", "category": "美容液",
        "ingredient_focus": ["retinoid"], "active_ingredients": ["retinol"],
        "ingredient_strength": {}, "use_days": use_days if use_days is not None else [],
    }


def _aha_bha_step(use_days=None):
    return {
        "product": "BHA洗顔料", "category": "洗顔",
        "ingredient_focus": ["aha_bha"], "active_ingredients": ["bha"],
        "ingredient_strength": {}, "use_days": use_days if use_days is not None else [],
    }


def _high_vc_step(use_days=None):
    return {
        "product": "高濃度ビタミンC美容液", "category": "美容液",
        "ingredient_focus": ["vitamin_c"], "active_ingredients": ["vitamin_c"],
        "ingredient_strength": {"vitamin_c": "high"}, "use_days": use_days if use_days is not None else [],
    }


def _azelaic_step(use_days=None):
    return {
        "product": "アゼライン酸化粧水", "category": "化粧水",
        "ingredient_focus": ["azelaic_acid"], "active_ingredients": ["azelaic_acid"],
        "ingredient_strength": {}, "use_days": use_days if use_days is not None else [],
    }


def _high_niacinamide_step(use_days=None):
    return {
        "product": "高濃度ナイアシンアミド美容液", "category": "美容液",
        "ingredient_focus": ["niacinamide"], "active_ingredients": ["niacinamide"],
        "ingredient_strength": {"niacinamide": "high"}, "use_days": use_days if use_days is not None else [],
    }


def _gentle_step():
    return {
        "product": "保湿クリーム", "category": "クリーム",
        "ingredient_focus": ["ceramide"], "active_ingredients": ["ceramide"],
        "ingredient_strength": {}, "use_days": [],
    }


def _base_data(night_steps, hydration=40, barrier=40, premium_improvement_priority=None):
    return {
        "scores": {"hydration": hydration, "barrier": barrier},
        "night": {"steps": night_steps},
        "premium_improvement_priority": premium_improvement_priority or [],
    }


class ClassifyHighStimFamilyTests(unittest.TestCase):
    def test_retinoid_is_family_b(self):
        self.assertEqual(app._classify_high_stim_family(_retinoid_step()), "B")

    def test_aha_bha_is_family_c(self):
        self.assertEqual(app._classify_high_stim_family(_aha_bha_step()), "C")

    def test_azelaic_is_family_d(self):
        self.assertEqual(app._classify_high_stim_family(_azelaic_step()), "D")

    def test_high_vitamin_c_is_family_a(self):
        self.assertEqual(app._classify_high_stim_family(_high_vc_step()), "A")

    def test_low_strength_vitamin_c_is_not_classified(self):
        step = _high_vc_step()
        step["ingredient_strength"] = {"vitamin_c": "low"}
        self.assertIsNone(app._classify_high_stim_family(step))

    def test_high_niacinamide_is_family_e(self):
        self.assertEqual(app._classify_high_stim_family(_high_niacinamide_step()), "E")

    def test_gentle_product_is_not_classified(self):
        self.assertIsNone(app._classify_high_stim_family(_gentle_step()))


class RestrictHighStimFamiliesTriggerConditionTests(unittest.TestCase):
    def test_noop_when_hydration_missing(self):
        data = _base_data([_retinoid_step(), _high_vc_step()], hydration=None, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertNotIn("deferred_steps", result)
        self.assertEqual(len(result["night"]["steps"]), 2)

    def test_noop_when_barrier_above_50(self):
        data = _base_data([_retinoid_step(), _high_vc_step()], hydration=40, barrier=60)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertNotIn("deferred_steps", result)

    def test_noop_when_hydration_above_50(self):
        data = _base_data([_retinoid_step(), _high_vc_step()], hydration=60, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertNotIn("deferred_steps", result)

    def test_noop_when_only_one_high_stim_family_present(self):
        data = _base_data([_retinoid_step(), _gentle_step()], hydration=40, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertNotIn("deferred_steps", result)
        self.assertEqual(len(result["night"]["steps"]), 2)


class RestrictHighStimFamiliesBxCExclusionTests(unittest.TestCase):
    def test_retinoid_and_aha_bha_alone_are_left_untouched(self):
        # B×Cは既存の専用ロジック(頻度調整)に委ねるため、本ロジックの対象外。
        data = _base_data([_retinoid_step(), _aha_bha_step()], hydration=40, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertNotIn("deferred_steps", result)
        self.assertEqual(len(result["night"]["steps"]), 2)

    def test_third_family_alongside_protected_bc_pair_is_also_left_untouched(self):
        # B・Cが保護ペアとして除外された結果、残り(A)が1系統のみとなるため
        # 制限は発動しない(意図的な挙動: Cと競合する「2つ目の独立系統」が
        # 無いため)。
        data = _base_data(
            [_retinoid_step(), _aha_bha_step(), _high_vc_step()], hydration=40, barrier=40
        )
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertNotIn("deferred_steps", result)
        self.assertEqual(len(result["night"]["steps"]), 3)


class RestrictHighStimFamiliesDeferralTests(unittest.TestCase):
    def test_two_unprotected_families_defer_the_non_kept_one(self):
        data = _base_data([_high_vc_step(), _azelaic_step()], hydration=40, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertEqual(len(result["night"]["steps"]), 1)
        self.assertEqual(len(result["deferred_steps"]), 1)

    def test_deferred_entry_preserves_product_and_adds_metadata(self):
        data = _base_data([_high_vc_step(), _azelaic_step()], hydration=40, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        deferred = result["deferred_steps"][0]
        self.assertIn(deferred["product"], ("高濃度ビタミンC美容液", "アゼライン酸化粧水"))
        self.assertIn(deferred["deferred_family"], ("A", "D"))
        self.assertTrue(deferred["deferred_family_label"])
        self.assertEqual(deferred["deferred_reason"], "現在は使用を控え、肌状態が改善した場合に導入を検討します。")

    def test_deferred_steps_accumulate_with_existing_entries(self):
        data = _base_data([_high_vc_step(), _azelaic_step()], hydration=40, barrier=40)
        data["deferred_steps"] = [{"product": "既存の保留商品"}]
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        self.assertEqual(len(result["deferred_steps"]), 2)
        self.assertEqual(result["deferred_steps"][0]["product"], "既存の保留商品")

    def test_gentle_non_high_stim_steps_are_never_deferred(self):
        data = _base_data(
            [_high_vc_step(), _azelaic_step(), _gentle_step()], hydration=40, barrier=40
        )
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        kept_products = [s["product"] for s in result["night"]["steps"]]
        self.assertIn("保湿クリーム", kept_products)
        deferred_products = [s["product"] for s in result["deferred_steps"]]
        self.assertNotIn("保湿クリーム", deferred_products)


class RestrictHighStimFamiliesPriorityOrderTests(unittest.TestCase):
    def test_higher_priority_concern_family_is_kept(self):
        # azelaic_acid(D)の対応悩み = acne/redness/dullness/tone_evenness
        # vitamin_c高濃度(A)の対応悩み = dullness/tone_evenness/firmness
        # "redness"をrank1にすることで、Dのみが一致する軸を最優先にする。
        priority = [
            {"key": "redness", "rank": 1},
            {"key": "firmness", "rank": 2},
        ]
        data = _base_data(
            [_high_vc_step(), _azelaic_step()], hydration=40, barrier=40,
            premium_improvement_priority=priority,
        )
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        kept_products = [s["product"] for s in result["night"]["steps"]]
        self.assertEqual(kept_products, ["アゼライン酸化粧水"])
        deferred_families = [s["deferred_family"] for s in result["deferred_steps"]]
        self.assertEqual(deferred_families, ["A"])

    def test_safety_risk_breaks_tie_when_priority_equal(self):
        # 優先順位情報が無い(タイ)場合、infer_active_profileのirritation_risk/
        # strengthがより低い(穏やかな)方を残す。
        data = _base_data([_high_vc_step(), _azelaic_step()], hydration=40, barrier=40)
        result = app.restrict_high_stim_families_for_fragile_skin(data)
        kept_step = result["night"]["steps"][0]
        kept_family = app._classify_high_stim_family(kept_step)
        vc_risk = app._irritation_risk_rank(_high_vc_step())
        azelaic_risk = app._irritation_risk_rank(_azelaic_step())
        expected_family = "A" if vc_risk <= azelaic_risk else "D"
        self.assertEqual(kept_family, expected_family)


if __name__ == "__main__":
    unittest.main()
