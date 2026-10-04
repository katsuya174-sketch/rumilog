"""
Phase 3「AIによる商品事前収集」の本実装前の最小技術検証(PoC)用モジュール。

目的: 既存google-genai 1.66.0 + 既存Gemini API環境で、
  検索 → 出典取得 → 商品情報抽出 → 構造化JSON → usage取得
を1回のAPI呼び出しで完結できるかを確認する。

このモジュールは診断パイプライン・product_masterには一切接続しない
(検証専用、呼び出しも最大3商品までの手動実行を想定)。
"""

from google.genai import types

# formulation_featuresの統制語彙(合意事項)。"other"の場合のみ
# other_detailへメーカー等が使用する技術名称・根拠を保持する。
FORMULATION_FEATURE_VALUES = [
    "liposome", "encapsulated", "nano", "sustained_release",
    "stabilized", "derivative", "other", "unknown",
]

CONFIDENCE_VALUES = ["high", "medium", "low", "unknown"]


def build_product_collection_prompt(brand_hint, product_name):
    """
    公式メーカー/ブランド情報を最優先し、推測を禁止するプロンプト。
    不明な情報はunknownとすることを明記する。
    """
    return f"""あなたは化粧品・スキンケア製品の情報調査アシスタントです。
以下の製品について、Web検索で調査し、公式メーカー/ブランド情報を
最優先の根拠として、構造化された情報を返してください。

製品: {brand_hint} {product_name}

厳守事項:
- 推測で成分・濃度・製剤特徴を記載することは絶対禁止。確認できない情報は
  必ず"unknown"とすること(存在しそうだから書く、は禁止)。
- 可能な限り公式メーカー/ブランドの公式サイト・公式カタログ等を優先し、
  それ以外の情報源(販売店・レビューサイト等)を使った場合は出典を明記する。
- 各項目について、実際に参照したページのURLをsource_urlに記載すること。
  URLが分からない場合はsource_urlも"unknown"とする。
- formulation_features(リポソーム化・カプセル化・ナノ化・徐放性・
  安定化技術・誘導体)は該当する場合のみ列挙し、該当しない/不明な場合は
  含めない。該当するが上記の分類に当てはまらない独自技術の場合は
  feature="other"とし、other_detailにメーカーが使用する技術名称と
  その根拠(出典)を記載する。
"""


def build_product_collection_response_schema():
    """
    厳格な構造化出力スキーマ。成分・formulation_featuresは配列で、
    各要素にconfidence・source_urlを持たせ、複数ソース一致/情報不足/
    矛盾を後工程で判定できるようにする。
    """
    ingredient_item_schema = types.Schema(
        type="OBJECT",
        properties={
            "ingredient": types.Schema(type="STRING"),
            "concentration": types.Schema(
                type="STRING",
                description="確認できた濃度(%等)。不明な場合は'unknown'",
            ),
            "confidence": types.Schema(type="STRING", enum=CONFIDENCE_VALUES),
            "source_url": types.Schema(type="STRING"),
        },
        required=["ingredient", "concentration", "confidence", "source_url"],
    )

    formulation_item_schema = types.Schema(
        type="OBJECT",
        properties={
            "feature": types.Schema(type="STRING", enum=FORMULATION_FEATURE_VALUES),
            "other_detail": types.Schema(
                type="STRING",
                description="featureが'other'の場合のみ、メーカーが使用する技術名称・根拠。それ以外は'unknown'",
            ),
            "confidence": types.Schema(type="STRING", enum=CONFIDENCE_VALUES),
            "source_url": types.Schema(type="STRING"),
        },
        required=["feature", "other_detail", "confidence", "source_url"],
    )

    return types.Schema(
        type="OBJECT",
        properties={
            "brand": types.Schema(type="STRING"),
            "product_name": types.Schema(type="STRING"),
            "jan_code": types.Schema(
                type="STRING",
                description="JANコード(数字)。不明な場合は'unknown'",
            ),
            "active_ingredients": types.Schema(type="ARRAY", items=ingredient_item_schema),
            "formulation_features": types.Schema(type="ARRAY", items=formulation_item_schema),
            "official_source_confirmed": types.Schema(
                type="BOOLEAN",
                description="公式メーカー/ブランドの情報で確認できたか",
            ),
        },
        required=[
            "brand", "product_name", "jan_code",
            "active_ingredients", "formulation_features", "official_source_confirmed",
        ],
    )


def build_product_collection_config():
    """Google Search Grounding + 厳格JSON出力を同時に指定したconfig。
    この両立可否自体が今回の検証対象のため、ここで一箇所にまとめる。"""
    return types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        response_mime_type="application/json",
        response_schema=build_product_collection_response_schema(),
    )


def extract_usage_summary(response):
    """response.usage_metadataから、費用算出に必要な情報を取り出す。
    フィールドが存在しない場合はNoneを許容する(SDKバージョン差異対策)。"""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    return {
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        "tool_use_prompt_token_count": getattr(usage, "tool_use_prompt_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    }


def extract_grounding_summary(response):
    """response.candidates[0].grounding_metadataから、検索回数・出典URLを
    取り出す。grounding_metadataが無い場合は空の集計を返す。"""
    try:
        candidate = response.candidates[0]
        grounding = getattr(candidate, "grounding_metadata", None)
    except (AttributeError, IndexError):
        grounding = None

    if grounding is None:
        return {"web_search_queries": [], "sources": []}

    queries = list(getattr(grounding, "web_search_queries", None) or [])
    sources = []
    for chunk in (getattr(grounding, "grounding_chunks", None) or []):
        web = getattr(chunk, "web", None)
        if web is not None:
            sources.append({
                "uri": getattr(web, "uri", ""),
                "title": getattr(web, "title", ""),
                "domain": getattr(web, "domain", ""),
            })
    return {"web_search_queries": queries, "sources": sources}
