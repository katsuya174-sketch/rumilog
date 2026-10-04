"""
Grounding単体の切り分け診断(Phase 3本実装前、追加の最小検証)。

- response_schemaを外した状態でGoogle Search Groundingが実行されるかを
  まず確認する。実行されなければ、公式にSearch Grounding対応が明記されて
  いるモデルへ切り替えて再確認する。確認できたら、そのモデルで
  Grounding+Structured Outputsの併用を再検証する。
- モデルが生成したテキスト中のURL文字列は出典として採用しない。
  response.candidates[0].grounding_metadataに実在するcitation/
  grounding_chunksのURLのみを真正な出典として扱う。
- 追加API呼び出しは最大3回まで(合意事項)。

診断パイプライン・product_masterには一切接続しない。
"""

import json
import time

import app
from google.genai import types

import product_collection_grounding_poc as poc

TEST_PRODUCT = ("COSRX", "アドバンスドスネイルムチンエッセンス96")

call_count = 0
MAX_CALLS = 3


def call_and_report(label, model, use_schema):
    global call_count
    if call_count >= MAX_CALLS:
        print(f"[SKIP] {label}: 追加APIコール上限({MAX_CALLS}回)に達したため実行しません。")
        return None
    call_count += 1

    brand, name = TEST_PRODUCT
    prompt = poc.build_product_collection_prompt(brand, name)

    if use_schema:
        config = poc.build_product_collection_config()
    else:
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )

    print(f"\n===== [{call_count}/{MAX_CALLS}] {label} (model={model}, schema={use_schema}) =====")
    t_start = time.time()
    try:
        response = app.call_gemini_with_retry(
            app.client, model, prompt, config=config, max_retries=1, timeout=60,
        )
    except Exception as e:
        print(f"[EXCEPTION] {repr(e)}")
        return None
    elapsed = time.time() - t_start

    grounding = poc.extract_grounding_summary(response)
    usage = poc.extract_usage_summary(response)
    has_real_grounding = bool(grounding["web_search_queries"] or grounding["sources"])

    print(f"[ELAPSED] {elapsed:.1f}s")
    print(f"[USAGE] {usage}")
    print(f"[GROUNDING web_search_queries] {grounding['web_search_queries']}")
    print(f"[GROUNDING sources(real citations only)] {grounding['sources']}")
    print(f"[REAL SEARCH EVIDENCE PRESENT] {has_real_grounding}")

    if use_schema:
        try:
            parsed = json.loads(response.text)
            print(f"[PARSED JSON] {json.dumps(parsed, ensure_ascii=False, indent=2)[:800]}")
        except Exception as e:
            print(f"[JSON PARSE FAILED] {repr(e)}")
    else:
        print(f"[RAW TEXT] {getattr(response, 'text', '')[:500]}")

    return {"has_real_grounding": has_real_grounding, "grounding": grounding, "usage": usage}


def main():
    # 1. response_schemaを外し、現行モデル(gemini-3.1-flash-lite)単体で
    #    Groundingが実行されるか確認する。
    result1 = call_and_report(
        "Step1: schema無し・現行モデル", app.DETAIL_MODEL, use_schema=False
    )

    result3 = None
    if result1 and not result1["has_real_grounding"]:
        # 3. 検索されなければ、公式にGrounding対応が明記されているモデル
        #    (gemini-3.8-flash、2026-10時点のGoogle公式ドキュメントで確認済み)
        #    へ切り替えて再検証する。
        result3 = call_and_report(
            "Step3: schema無し・対応明記モデルへ変更", "gemini-3.8-flash", use_schema=False
        )

    result4 = None
    confirmed_model = None
    if result1 and result1["has_real_grounding"]:
        confirmed_model = app.DETAIL_MODEL
    elif result3 and result3["has_real_grounding"]:
        confirmed_model = "gemini-3.8-flash"

    if confirmed_model:
        # 4. Searchが確認できたモデルで、Grounding+Structured Outputsの
        #    併用を再検証する。
        result4 = call_and_report(
            "Step4: schema併用・検索確認済みモデル", confirmed_model, use_schema=True
        )

    print("\n===== DIAGNOSTIC SUMMARY =====")
    print(f"Step1 (現行モデル, schema無し) 実検索あり: {result1['has_real_grounding'] if result1 else 'N/A(未実行)'}")
    print(f"Step3 (対応明記モデル, schema無し) 実検索あり: {result3['has_real_grounding'] if result3 else 'N/A(未実行)'}")
    print(f"Step4 (schema併用) 実検索あり: {result4['has_real_grounding'] if result4 else 'N/A(未実行)'}")
    print(f"合計APIコール数: {call_count}")


if __name__ == "__main__":
    main()
