PRODUCT_IMAGES = {
    "クレンジング": "cleanser.jpg",
    "洗顔": "cleanser.jpg",
    "ブースター": "serum.jpg",
    "化粧水": "toner.jpg",
    "美容液": "serum.jpg",
    "クリーム": "cream.jpg",
    "乳液": "cream.jpg",
    "日焼け止め": "sunscreen.jpg",
    "パック": "mask.jpg",
    "ピーリング": "peeling.jpg"
}
CATEGORY_TAGS = [
    "クレンジング",
    "洗顔",
    "化粧水",
    "美容液",
    "ブースター",
    "乳液",
    "クリーム",
    "日焼け止め",
    "パック",
    "ピーリング",
]
RETINOL_LEVEL_RULE = {
    0: "レチノイドなし",
    1: "初心者向け",
    2: "中級者向け",
    3: "上級者向け"
}
SENSITIVE_OK_VALUES = [
    "yes",
    "no",
    "unknown",
]

SKIN_TYPE_TAGS = [
    "dry",
    "oily",
    "mixed",
    "normal",   
    "sensitive",
]
INGREDIENT_STRENGTH_VALUES = [
    "high",
    "medium",
    "low"
]
MAIN_FUNCTION_MAP = {
    "barrier": "バリア強化",
    "repair": "バリア修復",
    "soothing": "鎮静ケア",
    "hydration": "保湿",
    "aging": "エイジングケア",
    "firming": "ハリ改善",
    "uv_protection": "紫外線防御",
    "sun_protection": "紫外線防御",
    "tone_up": "トーンアップ",
    "brightening": "透明感向上",
    "whitening": "美白ケア",
    "pores": "毛穴改善",
    "acne": "ニキビ予防",
    "oil_control": "皮脂抑制",
    "texture": "キメ改善",
    "smooth": "なめらか肌",
    "dryness": "保湿",
    "makeup_removal": "メイク落とし",
    "sunscreen_removal": "日焼け止めオフ",
    "sebum_cleansing": "皮脂汚れオフ",
    "blackhead_prevention": "黒ずみ予防",
    "pore_preventive": "毛穴詰まり予防",
    "non_stripping": "洗いすぎ防止",
    "barrier_preserving": "うるおいを守って洗う"
}
MAIN_FUNCTION_TAGS = set(MAIN_FUNCTION_MAP.values())

CLEANSING_TAGS = {
    "makeup_removal":"メイク除去",
    "sunscreen_removal":"日焼け止めオフ",
    "sebum_cleansing":"皮脂汚れ除去",
    "blackhead_prevention":"黒ずみ予防",
    "pore_preventive":"毛穴詰まり予防",
    "low_friction":"低摩擦",
    "easy_rinse":"素早い洗い流し",
    "non_stripping":"マイルドな洗浄力",
    "barrier_preserving":"バリア保護機能",
    "residue_free":"すっきりした洗い上がり",
    "heavy_makeup_ok":"しっかりメイク対応",
    "light_makeup_ok":"ナチュラルメイク対応",
    "daily_use_friendly":"毎日使いやすい",
    "morning_cleanse_ok":"朝クレンジング",
    "essential_oil":"天然香料",
}
 # 処方技術
formulation_labels = {
    "liposome": "リポソーム処方",
    "encapsulation": "カプセル化処方",
    "capsule": "カプセル化処方",
    "mild_formula": "低刺激寄りの処方",
    "low_ph": "低pH設計",
    "barrier_formula": "バリア重視の処方",
    "essence_texture": "軽めで使いやすい質感",
    "cream_texture": "しっとりした質感",
    "waterproof": "落ちにくい設計",
    "tone_up": "トーンアップ系",
    "acid_toner": "角質ケア系処方",
    "pad": "拭き取りしやすい設計",
    "powder_formula": "パウダー洗顔処方",
    "gel_formula": "ジェル処方",
    "gommage": "ゴマージュ処方",
    "luxury_formula": "リッチな処方",
    "nano_capsule":"ナノカプセル処方",
    "fermentation":"発酵ベース処方",
    "derma_formula": "ダーマコスメ寄り処方",
    "stabilized_vitamin_c": "安定化ビタミンC処方",
    "low_irritation": "刺激を抑えた設計",
    "fermentation":"発酵ベース処方",
    "non_comedogenic": "ノンコメドジェニック設計",
    }

CLEANSING_FORMULATION_TAGS = [
    "gel_formula",
    "oil_formula",
    "balm_formula",
    "milk_formula",
    "water_formula",
    "gel_to_oil_transform",
    "low_irritation",
    "low_friction_system",
    "easy_rinse_system",
    "barrier_preserving",
    "non_comedogenic",
]
technology_labels = {
    "liposome": "リポソーム技術で浸透性を高めています",
    "encapsulation": "成分をカプセル化し刺激を抑えています",
    "nano_capsule": "ナノレベルで浸透を高めています",
    "stabilized_vitamin_c": "安定化ビタミンCを採用しています",
    "fermentation": "発酵成分で吸収性を高めています",
    "derma_formula": "皮膚科学ベースの処方です",
    "low_irritation": "刺激を抑えた設計です",
    "microbiome_tech": "マイクロバイオーム発想の技術です",
    "slow_release": "成分を穏やかに届ける設計です",
    "low_friction_system": "摩擦を抑えながら落としやすい設計です",
    "gel_to_oil_transform": "ジェルがオイル状に変化してなじみやすい設計です",
    "easy_rinse_system": "すすぎやすさを意識した設計です",
    "barrier_preserving": "必要なうるおいを守りながら洗う設計です",
    "liposome": "リポソーム技術で浸透性を高めています",
    "encapsulation": "成分をカプセル化し刺激を抑えています",
    "nano_capsule": "ナノレベルで浸透を高めています",
    "stabilized_vitamin_c": "安定化ビタミンCを採用しています",
    "fermentation": "発酵成分で吸収性を高めています",
    "derma_formula": "皮膚科学ベースの処方です",
    "microbiome_tech": "マイクロバイオーム発想の技術です",
    "slow_release": "成分を穏やかに届ける設計です",

    # 洗浄系
    "low_friction_system": "摩擦を抑えながら落としやすい設計です",
    "gel_to_oil_transform": "ジェルがオイル状に変化してなじみやすい設計です",
    "easy_rinse_system": "すすぎやすさを意識した設計です",

    # 追加（重要）
    "carbonated_foam": "炭酸泡で角層への浸透を高める技術です",
    "oil_water_layer": "オイルと水の二層構造で保湿とツヤを同時に与えます",
    "exosome": "細胞間伝達に着目した先端美容技術です",
    "retinol_stabilization": "レチノールを安定化し刺激を抑える技術です",
    "enzyme_stabilization": "酵素の働きを安定させた処方です"

}
INGREDIENT_TAGS = [
    # =========================
    # 攻め・美白・透明感
    # =========================
    "vitamin_c",
    "vitamin_e",
    "tocopherol",
    "niacinamide",
    "tranexamic_acid",
    "alpha_arbutin",
    "arbutin",
    "glutathione",
    "kojic_acid",
    "ferulic_acid",
    "cysteamine",

    # =========================
    # レチノイド・ハリ・再生
    # =========================
    "retinol",
    "retinal",
    "retinoid",
    "bakuchiol",
    "peptide",
    "egf",
    "fgf",
    "pdrn",
    "adenosine",
    "collagen",
    "elastin",
    "coenzyme_q10",

    # =========================
    # 保湿・バリア
    # =========================
    "ceramide",
    "cholesterol",
    "fatty_acid",
    "hyaluronic_acid",
    "polyglutamic_acid",
    "beta_glucan",
    "panthenol",
    "allantoin",
    "squalane",
    "amino_acid",
    "urea",
    "glycerin",
    "trehalose",
    "ectoin",
    "nmf",
    "mucin",
    "snail",

    # =========================
    # 鎮静・抗炎症
    # =========================
    "cica",
    "teca",
    "madecassoside",
    "centella_extract",
    "heartleaf",
    "mugwort",
    "propolis",
    "dipotassium_glycyrrhizate",
    "azulene",
    "calamine",

    # =========================
    # 角質・毛穴・皮脂
    # =========================
    "azelaic_acid",
    "aha",
    "bha",
    "pha",
    "lha",
    "salicylic_acid",
    "glycolic_acid",
    "lactic_acid",
    "mandelic_acid",
    "gluconolactone",
    "succinic_acid",
    "enzyme",
    "papain",
    "bromelain",
    "clay",
    "charcoal",
    "zinc",
    "sulfur",

    # =========================
    # 発酵
    # =========================
    "probiotic_ferment",
    "bifida",
    "galactomyces",
    "saccharomyces",
    "lactobacillus",

    # =========================
    # UV
    # =========================
    "uv_filter",
    "zinc_oxide",
    "titanium_dioxide",

    # =========================
    # 抗酸化・補助
    # =========================
    "caffeine",
    "resveratrol",
    "idebenone",

    # =========================
    # オイル系
    # =========================
    "mineral_oil",
    "ester_oil",
    "plant_oil",
    "jojoba_oil",
    "olive_oil",
    "argan_oil",
    "sunflower_oil",
    "grapeseed_oil",
    "rosehip_oil",
    "tea_tree_oil",

    # =========================
    # 独自成分・独自複合体
    # =========================
    "rice_power_no11",
    "rice_power_no6",
    "multi_ceramide_complex",
    "ceramide_complex_ex",
    "derma_barrier_complex",
    "moisture_lock_complex",
    "hyaluronic_5d_complex",
    "aqua_sphere_complex",
    "ectoin_protect_complex",
    "madewhite",
    "melazero",
    "melazero_v2",
    "white_tranex_complex",
    "tone_up_complex",
    "gluta_bright_complex",
    "vitamin_c_booster_complex",
    "dark_spot_corrector_complex",
    "pore_refining_complex",
    "pore_minimizing_complex",
    "sebum_control_complex",
    "oil_balancing_complex",
    "anti_shine_complex",
    "blackhead_clear_complex",
    "clay_detox_complex",
    "cica_complex",
    "cica_reedle_complex",
    "centella_complex",
    "centella_asiatica_5x",
    "heartleaf_complex",
    "soothing_complex",
    "anti_redness_complex",
    "calming_barrier_complex",
    "acne_clear_complex",
    "anti_acne_complex",
    "trouble_care_complex",
    "spot_control_complex",
    "blemish_control_complex",
    "peptide_complex",
    "peptide_complex_5",
    "collagen_boost_complex",
    "firming_complex",
    "elasticity_complex",
    "retinol_booster_complex",
    "retinal_repair_complex",
    "lifting_complex",
    "bifida_complex",
    "galactomyces_complex",
    "fermented_yeast_complex",
    "probiotic_complex",
    "microbiome_complex",
    "derma_complex",
    "skin_repair_complex",
    "multi_care_complex",
    "total_skin_solution_complex",
]
# =========================
# 表示用ラベル辞書
# =========================
ingredient_map = {
    # =========================
    # 攻め・美白・透明感
    # =========================
    "vitamin_c": "ビタミンC",
    "vitamin_e": "ビタミンE",
    "tocopherol": "トコフェロール",
    "niacinamide": "ナイアシンアミド",
    "tranexamic_acid": "トラネキサム酸",
    "alpha_arbutin": "アルファアルブチン",
    "arbutin": "アルブチン",
    "glutathione": "グルタチオン",
    "kojic_acid": "コウジ酸",
    "ferulic_acid": "フェルラ酸",
    "cysteamine": "システアミン",

    # =========================
    # レチノイド・ハリ・再生
    # =========================
    "retinol": "レチノール",
    "retinal": "レチナール",
    "retinoid": "レチノイド",
    "bakuchiol": "バクチオール",
    "peptide": "ペプチド",
    "egf": "EGF",
    "fgf": "FGF",
    "pdrn": "PDRN",
    "adenosine": "アデノシン",
    "collagen": "コラーゲン",
    "elastin": "エラスチン",
    "coenzyme_q10": "コエンザイムQ10",

    # =========================
    # 保湿・バリア
    # =========================
    "ceramide": "セラミド",
    "cholesterol": "コレステロール",
    "fatty_acid": "脂肪酸",
    "hyaluronic_acid": "ヒアルロン酸",
    "polyglutamic_acid": "ポリグルタミン酸",
    "beta_glucan": "βグルカン",
    "panthenol": "パンテノール",
    "allantoin": "アラントイン",
    "squalane": "スクワラン",
    "amino_acid": "アミノ酸",
    "urea": "尿素",
    "glycerin": "グリセリン",
    "trehalose": "トレハロース",
    "ectoin": "エクトイン",
    "nmf": "NMF",
    "mucin": "ムチン",
    "snail": "スネイル",

    # =========================
    # 鎮静・抗炎症
    # =========================
    "cica": "CICA",
    "teca": "TECA",
    "madecassoside": "マデカッソシド",
    "centella_extract": "ツボクサエキス",
    "heartleaf": "ドクダミ",
    "mugwort": "ヨモギ",
    "propolis": "プロポリス",
    "dipotassium_glycyrrhizate": "グリチルリチン酸2K",
    "azulene": "アズレン",
    "calamine": "カラミン",

    # =========================
    # 角質・毛穴・皮脂
    # =========================
    "azelaic_acid": "アゼライン酸",
    "aha": "AHA",
    "bha": "BHA",
    "pha": "PHA",
    "lha": "LHA",
    "salicylic_acid": "サリチル酸",
    "glycolic_acid": "グリコール酸",
    "lactic_acid": "乳酸",
    "mandelic_acid": "マンデル酸",
    "gluconolactone": "グルコノラクトン",
    "succinic_acid": "コハク酸",
    "enzyme": "酵素",
    "papain": "パパイン",
    "bromelain": "ブロメライン",
    "clay": "クレイ",
    "charcoal": "炭",
    "zinc": "亜鉛",
    "sulfur": "硫黄",

    # =========================
    # 発酵
    # =========================
    "probiotic_ferment": "発酵成分",
    "bifida": "ビフィズス",
    "galactomyces": "ガラクトミセス",
    "saccharomyces": "サッカロミセス",
    "lactobacillus": "乳酸菌",

    # =========================
    # UV
    # =========================
    "uv_filter": "UVフィルター",
    "zinc_oxide": "酸化亜鉛",
    "titanium_dioxide": "酸化チタン",

    # =========================
    # 抗酸化・補助
    # =========================
    "caffeine": "カフェイン",
    "resveratrol": "レスベラトロール",
    "idebenone": "イデベノン",

    # =========================
    # オイル系
    # =========================
    "mineral_oil": "ミネラルオイル",
    "ester_oil": "エステルオイル",
    "plant_oil": "植物オイル",
    "jojoba_oil": "ホホバオイル",
    "olive_oil": "オリーブオイル",
    "argan_oil": "アルガンオイル",
    "sunflower_oil": "ヒマワリ種子油",
    "grapeseed_oil": "グレープシードオイル",
    "rosehip_oil": "ローズヒップオイル",
    "tea_tree_oil": "ティーツリーオイル",

    # =========================
    # 独自成分・独自複合体
    # =========================
    "rice_power_no11": "ライスパワーNo.11",
    "rice_power_no6": "ライスパワーNo.6",
    "multi_ceramide_complex": "マルチセラミド複合体",
    "ceramide_complex_ex": "セラミド複合体EX",
    "derma_barrier_complex": "ダーマバリア複合体",
    "moisture_lock_complex": "モイスチャーロック複合体",
    "hyaluronic_5d_complex": "5種ヒアルロン酸複合体",
    "aqua_sphere_complex": "アクアスフィア複合体",
    "ectoin_protect_complex": "エクトイン保護複合体",
    "madewhite": "マデホワイト",
    "melazero": "メラゼロ",
    "melazero_v2": "メラゼロV2",
    "white_tranex_complex": "ホワイトトラネックス複合体",
    "tone_up_complex": "トーンアップ複合体",
    "gluta_bright_complex": "グルタブライト複合体",
    "vitamin_c_booster_complex": "ビタミンCブースター複合体",
    "dark_spot_corrector_complex": "ダークスポット補正複合体",
    "pore_refining_complex": "ポアリファイニング複合体",
    "pore_minimizing_complex": "ポアミニマイジング複合体",
    "sebum_control_complex": "皮脂コントロール複合体",
    "oil_balancing_complex": "皮脂バランス複合体",
    "anti_shine_complex": "アンチシャイン複合体",
    "blackhead_clear_complex": "ブラックヘッドクリア複合体",
    "clay_detox_complex": "クレイデトックス複合体",
    "cica_complex": "CICA複合体",
    "cica_reedle_complex": "CICAリードル複合体",
    "centella_complex": "ツボクサ複合体",
    "centella_asiatica_5x": "ツボクサ5X複合体",
    "heartleaf_complex": "ドクダミ複合体",
    "soothing_complex": "鎮静複合体",
    "anti_redness_complex": "赤みケア複合体",
    "calming_barrier_complex": "鎮静バリア複合体",
    "acne_clear_complex": "アクネクリア複合体",
    "anti_acne_complex": "抗ニキビ複合体",
    "trouble_care_complex": "トラブルケア複合体",
    "spot_control_complex": "スポットコントロール複合体",
    "blemish_control_complex": "ブレミッシュコントロール複合体",
    "peptide_complex": "ペプチド複合体",
    "peptide_complex_5": "5種ペプチド複合体",
    "collagen_boost_complex": "コラーゲンブースト複合体",
    "firming_complex": "ハリ強化複合体",
    "elasticity_complex": "弾力複合体",
    "retinol_booster_complex": "レチノールブースター複合体",
    "retinal_repair_complex": "レチナール補修複合体",
    "lifting_complex": "リフティング複合体",
    "bifida_complex": "ビフィズス複合体",
    "galactomyces_complex": "ガラクトミセス複合体",
    "fermented_yeast_complex": "発酵酵母複合体",
    "probiotic_complex": "プロバイオティクス複合体",
    "microbiome_complex": "マイクロバイオーム複合体",
    "derma_complex": "ダーマ複合体",
    "skin_repair_complex": "スキンリペア複合体",
    "multi_care_complex": "マルチケア複合体",
    "total_skin_solution_complex": "トータルスキンソリューション複合体",
}
signature_ingredient_effects = {
    # バリア・保湿
    "rice_power_no11": ["barrier", "dryness"],
    "rice_power_no6": ["oil_control"],
    "multi_ceramide_complex": ["barrier", "dryness"],
    "ceramide_complex_ex": ["barrier", "dryness"],
    "derma_barrier_complex": ["barrier", "dryness"],
    "moisture_lock_complex": ["dryness", "barrier"],
    "hyaluronic_5d_complex": ["dryness"],
    "aqua_sphere_complex": ["dryness"],
    "ectoin_protect_complex": ["barrier", "dryness"],

    # 美白・くすみ
    "madewhite": ["whitening", "dullness"],
    "melazero": ["whitening"],
    "melazero_v2": ["whitening", "dullness"],
    "white_tranex_complex": ["whitening"],
    "tone_up_complex": ["dullness", "whitening"],
    "gluta_bright_complex": ["whitening", "dullness"],
    "vitamin_c_booster_complex": ["whitening", "dullness"],
    "dark_spot_corrector_complex": ["whitening"],

    # 毛穴・皮脂
    "pore_refining_complex": ["pores"],
    "pore_minimizing_complex": ["pores"],
    "sebum_control_complex": ["oil_control", "pores"],
    "oil_balancing_complex": ["oil_control"],
    "anti_shine_complex": ["oil_control"],
    "blackhead_clear_complex": ["pores"],
    "clay_detox_complex": ["pores", "oil_control"],

    # 赤み・鎮静
    "cica_complex": ["redness"],
    "cica_reedle_complex": ["redness"],
    "centella_complex": ["redness", "barrier"],
    "centella_asiatica_5x": ["redness"],
    "heartleaf_complex": ["redness"],
    "soothing_complex": ["redness"],
    "anti_redness_complex": ["redness"],
    "calming_barrier_complex": ["redness", "barrier"],

    # ニキビ
    "acne_clear_complex": ["acne"],
    "anti_acne_complex": ["acne"],
    "trouble_care_complex": ["acne"],
    "spot_control_complex": ["acne"],
    "blemish_control_complex": ["acne"],

    # ハリ・エイジング
    "peptide_complex": ["aging"],
    "peptide_complex_5": ["aging"],
    "collagen_boost_complex": ["aging"],
    "firming_complex": ["aging"],
    "elasticity_complex": ["aging"],
    "retinol_booster_complex": ["aging"],
    "retinal_repair_complex": ["aging"],
    "lifting_complex": ["aging"],

    # 発酵
    "bifida_complex": ["barrier"],
    "galactomyces_complex": ["dullness"],
    "fermented_yeast_complex": ["barrier", "dullness"],
    "probiotic_complex": ["barrier"],
    "microbiome_complex": ["barrier"],

    # マルチ
    "derma_complex": ["barrier", "redness"],
    "skin_repair_complex": ["barrier", "aging"],
    "multi_care_complex": ["pores", "dullness", "aging"],
    "total_skin_solution_complex": ["pores", "redness", "aging"],

    
    "multi_ceramide_complex": ["barrier", "dryness"],
    "ceramide_complex_ex": ["barrier", "dryness"],
    "derma_barrier_complex": ["barrier", "dryness"],
    "moisture_lock_complex": ["dryness", "barrier"],
    "hyaluronic_5d_complex": ["dryness"],
    "aqua_sphere_complex": ["dryness"],
    "ectoin_protect_complex": ["barrier", "dryness"],

    "white_tranex_complex": ["whitening"],
    "tone_up_complex": ["dullness", "whitening"],
    "gluta_bright_complex": ["whitening", "dullness"],
    "vitamin_c_booster_complex": ["whitening", "dullness"],
    "dark_spot_corrector_complex": ["whitening"],

    "pore_refining_complex": ["pores"],
    "pore_minimizing_complex": ["pores"],
    "sebum_control_complex": ["oil_control", "pores"],
    "oil_balancing_complex": ["oil_control"],
    "anti_shine_complex": ["oil_control"],
    "blackhead_clear_complex": ["pores"],
    "clay_detox_complex": ["pores", "oil_control"],

    "cica_complex": ["redness"],
    "cica_reedle_complex": ["redness"],
    "centella_complex": ["redness", "barrier"],
    "centella_asiatica_5x": ["redness"],
    "heartleaf_complex": ["redness"],
    "soothing_complex": ["redness"],
    "anti_redness_complex": ["redness"],
    "calming_barrier_complex": ["redness", "barrier"],

    "acne_clear_complex": ["acne"],
    "anti_acne_complex": ["acne"],
    "trouble_care_complex": ["acne"],
    "spot_control_complex": ["acne"],
    "blemish_control_complex": ["acne"],

    "peptide_complex": ["aging"],
    "peptide_complex_5": ["aging"],
    "collagen_boost_complex": ["aging"],
    "firming_complex": ["aging"],
    "elasticity_complex": ["aging"],
    "retinol_booster_complex": ["aging"],
    "retinal_repair_complex": ["aging"],
    "lifting_complex": ["aging"],

    "bifida_complex": ["barrier"],
    "galactomyces_complex": ["dullness"],
    "fermented_yeast_complex": ["barrier", "dullness"],
    "probiotic_complex": ["barrier"],
    "microbiome_complex": ["barrier"],

    "derma_complex": ["barrier", "redness"],
    "skin_repair_complex": ["barrier", "aging"],
    "multi_care_complex": ["pores", "dullness", "aging"],
    "total_skin_solution_complex": ["pores", "redness", "aging"],

    }

texture_labels = {
    "light": "軽い使用感で朝も使いやすい",
    "rich": "しっとり重めで乾燥肌向き",
    "watery": "水のようにさっぱりした使用感",
    "gel": "みずみずしく軽いジェルタイプ",
    "cream": "コクのあるクリームタイプ",
    "essence": "美容液のような軽いテクスチャ",
    "medium": "軽すぎず、重すぎない",
    "oil": "オイルらしいなめらかな使用感",
    "balm": "バーム状で密着感がある",
    "foam": "泡立ちやすく洗顔向き",
    "powder": "パウダータイプで洗顔向き",
    "milky":""
}
contraindications_labels = {
    "sensitive_skin": "敏感肌の方は刺激を感じる可能性があります",
    "retinol_beginner": "レチノール初心者は注意が必要です",
    "retinol_same_routine": "レチノールとの併用は避けてください",
    "acid_same_routine": "ピーリング系との併用は注意が必要です",
    "high_irritation_risk": "刺激が出やすい成分設計です",
    "dry_skin_caution": "乾燥しやすい可能性があります",
    "redness_prone": "赤みが出やすい方は注意が必要です",
    "daily_use_caution": "毎日の使用は様子を見てください",
    "morning_use_caution": "朝の使用は避けるのがおすすめです",
    "oily_skin_caution": "脂性肌にはやや重く感じる可能性があります",
    "photosensitivity": "紫外線の影響を受けやすくなるため日中は注意してください",
    "retinol_pregnancy": "妊娠中・授乳中の使用は避けてください",
    "bee_product_allergy": "ハチ由来成分にアレルギーがある方は使用を避けてください",
    "essential_oil_caution": "精油成分に敏感な方は注意が必要です",
    "fungal_acne_caution": "脂質リッチなため肌質によっては合わない可能性があります",
    "heavy_makeup_not_enough": "濃いメイクには洗浄力が物足りない可能性があります",
    "rinse_required": "洗い流しを丁寧に行ってください",

}
signature_ingredient_labels = {
    "rice_power_no11": "独自のバリアケア成分を配合",
    "rice_power_no6": "独自の皮脂バランス成分を配合",
    "multi_ceramide_complex": "複合セラミド設計を採用",
    "ceramide_complex_ex": "セラミド複合体で保湿を強化",
    "derma_barrier_complex": "バリア重視の独自複合体を配合",
    "moisture_lock_complex": "うるおい保持に着目した複合体を配合",
    "hyaluronic_5d_complex": "多層ヒアルロン酸複合体を配合",
    "aqua_sphere_complex": "水分保持を狙った独自複合体を配合",
    "ectoin_protect_complex": "保護型の独自複合体を配合",
    "madewhite": "透明感ケア向けの独自成分を配合",
    "melazero": "ブライトニング発想の独自成分を配合",
    "melazero_v2": "ブライトニング複合体を配合",
    "white_tranex_complex": "美白発想の複合体を配合",
    "tone_up_complex": "トーンアップ発想の複合体を配合",
    "gluta_bright_complex": "グルタチオン系複合体を配合",
    "vitamin_c_booster_complex": "ビタミンC補強複合体を配合",
    "dark_spot_corrector_complex": "スポットケア向け複合体を配合",
    "pore_refining_complex": "毛穴ケア向け複合体を配合",
    "pore_minimizing_complex": "毛穴を引き締める発想の複合体を配合",
    "sebum_control_complex": "皮脂コントロール複合体を配合",
    "oil_balancing_complex": "油分バランス調整複合体を配合",
    "anti_shine_complex": "テカリ対策複合体を配合",
    "blackhead_clear_complex": "黒ずみ毛穴向け複合体を配合",
    "clay_detox_complex": "クレイ系浄化複合体を配合",
    "cica_complex": "CICA複合体を配合",
    "cica_reedle_complex": "CICA系独自複合体を配合",
    "centella_complex": "ツボクサ複合体を配合",
    "centella_asiatica_5x": "高濃度ツボクサ複合体を配合",
    "heartleaf_complex": "ドクダミ複合体を配合",
    "soothing_complex": "鎮静複合体を配合",
    "anti_redness_complex": "赤みケア複合体を配合",
    "calming_barrier_complex": "鎮静バリア複合体を配合",
    "acne_clear_complex": "アクネケア複合体を配合",
    "anti_acne_complex": "抗ニキビ複合体を配合",
    "trouble_care_complex": "肌トラブル向け複合体を配合",
    "spot_control_complex": "スポットケア複合体を配合",
    "blemish_control_complex": "ブレミッシュケア複合体を配合",
    "peptide_complex": "ペプチド複合体を配合",
    "peptide_complex_5": "5種ペプチド複合体を配合",
    "collagen_boost_complex": "コラーゲン発想の複合体を配合",
    "firming_complex": "ハリ感サポート複合体を配合",
    "elasticity_complex": "弾力ケア複合体を配合",
    "retinol_booster_complex": "レチノール補強複合体を配合",
    "retinal_repair_complex": "レチナール補修複合体を配合",
    "lifting_complex": "引き締め発想の複合体を配合",
    "bifida_complex": "ビフィズス複合体を配合",
    "galactomyces_complex": "ガラクトミセス複合体を配合",
    "fermented_yeast_complex": "発酵酵母複合体を配合",
    "probiotic_complex": "プロバイオティクス複合体を配合",
    "microbiome_complex": "マイクロバイオーム複合体を配合",
    "derma_complex": "ダーマ系複合体を配合",
    "skin_repair_complex": "スキンリペア複合体を配合",
    "multi_care_complex": "マルチケア複合体を配合",
    "total_skin_solution_complex": "総合ケア複合体を配合",
}
INGREDIENT_FOCUS_TAGS = [
    "毛穴ケア",
    "皮脂コントロール",
    "ニキビケア",
    "鎮静ケア",
    "赤みケア",
    "保湿",
    "バリア修復",
    "美白",
    "くすみ改善",
    "エイジングケア",
    "ハリ改善",
    "角質ケア",
    "ターンオーバー促進",
    "紫外線防御",
]
 # concern変換マップ
CONCERN_MAP = {

    # =========================
    # 毛穴系
    # =========================
    "pores": "pores",
    "texture": "pores",
    "clogged_pores": "pores",
    "blackheads": "pores",

    # =========================
    # ニキビ
    # =========================
    "acne": "acne",
    "breakout": "acne",
    "blemish": "acne",

    # =========================
    # 赤み・炎症
    # =========================
    "redness": "redness",
    "sensitivity": "redness",
    "soothing": "redness",
    "heat": "redness",
    "irritation": "redness",

    # =========================
    # 皮脂
    # =========================
    "oil": "oil_control",
    "oil_control": "oil_control",
    "sebum": "oil_control",
    "shine": "oil_control",

    # =========================
    # 乾燥・バリア
    # =========================
    "dryness": "dryness",
    "hydration": "dryness",
    "moisture": "dryness",

    "barrier": "barrier",
    "skin_barrier": "barrier",
    "repair": "barrier",

    # =========================
    # くすみ・美白
    # =========================
    "dullness": "dullness",
    "brightening": "dullness",
    "tone_up": "dullness",

    "whitening": "whitening",
    "dark_spot": "whitening",
    "hyperpigmentation": "whitening",
    "uv_damage": "whitening",

    # =========================
    # エイジング
    # =========================
    "aging": "aging",
    "wrinkle": "aging",
    "firmness": "aging",
    "elasticity": "aging",
    "lifting": "aging",

    # =========================
    # UV
    # =========================
    "uv_protection": "whitening",
    "sun_protection": "whitening",

    # =========================
    # 無視系（評価対象外）
    # =========================
    "penetration": None,
    "fragrance": None,
    "waterproof_makeup": None,
}
    # category変換マップ
AI_CATEGORY_MAP = {
    "cleansing oil": "クレンジング",
    "cleansing balm": "クレンジング",
    "cleansing water": "クレンジング",
    "cleansing milk": "クレンジング",
    "cleansing cream": "クレンジング",
    "makeup remover": "クレンジング",
    "remover": "クレンジング",
    "cleansing": "クレンジング",
    "cleanser": "洗顔",
    "face wash": "洗顔",
    "facewash": "洗顔",
    "foam cleanser": "洗顔",
    "gel cleanser": "洗顔",
    "powder cleanser": "洗顔",
    "toner": "化粧水",
    "skin": "化粧水",
    "serum": "美容液",
    "ampoule": "美容液",
    "essence": "美容液",
    "booster": "美容液",
    "cream": "クリーム",
    "moisturizer": "クリーム",
    "emulsion": "乳液",
    "milk": "乳液",
    "sunscreen": "日焼け止め",
    "sun cream": "日焼け止め",
    "suncream": "日焼け止め",
    "uv": "日焼け止め",
    "mask": "パック",
    "sheet mask": "パック",
    "peeling": "ピーリング",
    "exfoliator": "ピーリング",
    "exfoliating toner": "ピーリング"
    }
AI_INGREDIENT_MAP = {
    # =========================
    # ビタミンC・美白
    # =========================
    "vitamin c": "vitamin_c",
    "ascorbic acid": "vitamin_c",
    "l-ascorbic acid": "vitamin_c",
    "ascorbic acid 2-glucoside": "vitamin_c",
    "ascorbyl glucoside": "vitamin_c",
    "3-o-ethyl ascorbic acid": "vitamin_c",
    "ethyl ascorbic acid": "vitamin_c",
    "sodium ascorbyl phosphate": "vitamin_c",
    "magnesium ascorbyl phosphate": "vitamin_c",
    "tetrahexyldecyl ascorbate": "vitamin_c",
    "vitamin e": "vitamin_e",
    "tocopherol": "tocopherol",
    "niacinamide": "niacinamide",
    "tranexamic acid": "tranexamic_acid",
    "alpha arbutin": "alpha_arbutin",
    "arbutin": "arbutin",
    "glutathione": "glutathione",
    "kojic acid": "kojic_acid",
    "ferulic acid": "ferulic_acid",
    "cysteamine": "cysteamine",

    # =========================
    # レチノイド・ハリ
    # =========================
    "retinol": "retinol",
    "retinal": "retinal",
    "retinaldehyde": "retinal",
    "retinoid": "retinoid",
    "bakuchiol": "bakuchiol",
    "peptide": "peptide",
    "peptides": "peptide",
    "egf": "egf",
    "fgf": "fgf",
    "pdrn": "pdrn",
    "adenosine": "adenosine",
    "collagen": "collagen",
    "elastin": "elastin",
    "coenzyme q10": "coenzyme_q10",
    "ubiquinone": "coenzyme_q10",

    # =========================
    # 保湿・バリア
    # =========================
    "ceramide": "ceramide",
    "ceramides": "ceramide",
    "cholesterol": "cholesterol",
    "fatty acid": "fatty_acid",
    "fatty acids": "fatty_acid",
    "hyaluronic acid": "hyaluronic_acid",
    "sodium hyaluronate": "hyaluronic_acid",
    "hydrolyzed hyaluronic acid": "hyaluronic_acid",
    "polyglutamic acid": "polyglutamic_acid",
    "beta glucan": "beta_glucan",
    "β-glucan": "beta_glucan",
    "panthenol": "panthenol",
    "allantoin": "allantoin",
    "squalane": "squalane",
    "amino acid": "amino_acid",
    "amino acids": "amino_acid",
    "urea": "urea",
    "glycerin": "glycerin",
    "trehalose": "trehalose",
    "ectoin": "ectoin",
    "nmf": "nmf",
    "mucin": "mucin",
    "snail": "snail",
    "snail mucin": "snail",

    # =========================
    # 鎮静・抗炎症
    # =========================
    "cica": "cica",
    "teca": "teca",
    "madecassoside": "madecassoside",
    "centella": "centella_extract",
    "centella asiatica": "centella_extract",
    "centella asiatica extract": "centella_extract",
    "centella extract": "centella_extract",
    "heartleaf": "heartleaf",
    "houttuynia cordata": "heartleaf",
    "houttuynia cordata extract": "heartleaf",
    "mugwort": "mugwort",
    "artemisia": "mugwort",
    "artemisia princeps": "mugwort",
    "propolis": "propolis",
    "dipotassium glycyrrhizate": "dipotassium_glycyrrhizate",
    "glycyrrhizic acid dipotassium salt": "dipotassium_glycyrrhizate",
    "azulene": "azulene",
    "calamine": "calamine",

    # =========================
    # 角質・毛穴・皮脂
    # =========================
    "azelaic acid": "azelaic_acid",
    "aha": "aha",
    "alpha hydroxy acid": "aha",
    "alpha hydroxy acids": "aha",
    "bha": "bha",
    "beta hydroxy acid": "bha",
    "beta hydroxy acids": "bha",
    "pha": "pha",
    "polyhydroxy acid": "pha",
    "polyhydroxy acids": "pha",
    "lha": "lha",
    "salicylic acid": "salicylic_acid",
    "glycolic acid": "glycolic_acid",
    "lactic acid": "lactic_acid",
    "mandelic acid": "mandelic_acid",
    "gluconolactone": "gluconolactone",
    "succinic acid": "succinic_acid",
    "enzyme": "enzyme",
    "enzymes": "enzyme",
    "papain": "papain",
    "bromelain": "bromelain",
    "clay": "clay",
    "charcoal": "charcoal",
    "zinc": "zinc",
    "sulfur": "sulfur",

    # =========================
    # 発酵
    # =========================
    "probiotic ferment": "probiotic_ferment",
    "ferment": "probiotic_ferment",
    "bifida": "bifida",
    "bifida ferment lysate": "bifida",
    "galactomyces": "galactomyces",
    "galactomyces ferment filtrate": "galactomyces",
    "saccharomyces": "saccharomyces",
    "saccharomyces ferment filtrate": "saccharomyces",
    "lactobacillus": "lactobacillus",
    "lactobacillus ferment": "lactobacillus",

    # =========================
    # UV
    # =========================
    "uv filter": "uv_filter",
    "uv filters": "uv_filter",
    "zinc oxide": "zinc_oxide",
    "titanium dioxide": "titanium_dioxide",

    # =========================
    # 抗酸化・補助
    # =========================
    "caffeine": "caffeine",
    "resveratrol": "resveratrol",
    "idebenone": "idebenone",

    # =========================
    # オイル系
    # =========================
    "mineral oil": "mineral_oil",
    "ester oil": "ester_oil",
    "plant oil": "plant_oil",
    "plant oils": "plant_oil",
    "jojoba oil": "jojoba_oil",
    "olive oil": "olive_oil",
    "argan oil": "argan_oil",
    "sunflower oil": "sunflower_oil",
    "sunflower seed oil": "sunflower_oil",
    "grapeseed oil": "grapeseed_oil",
    "grape seed oil": "grapeseed_oil",
    "rosehip oil": "rosehip_oil",
    "tea tree oil": "tea_tree_oil",

    # =========================
    # 独自成分・独自複合体
    # =========================
    "rice power no.11": "rice_power_no11",
    "rice power no 11": "rice_power_no11",
    "rice power no.6": "rice_power_no6",
    "rice power no 6": "rice_power_no6",
    "multi ceramide complex": "multi_ceramide_complex",
    "ceramide complex ex": "ceramide_complex_ex",
    "derma barrier complex": "derma_barrier_complex",
    "moisture lock complex": "moisture_lock_complex",
    "5d hyaluronic complex": "hyaluronic_5d_complex",
    "hyaluronic 5d complex": "hyaluronic_5d_complex",
    "aqua sphere complex": "aqua_sphere_complex",
    "ectoin protect complex": "ectoin_protect_complex",
    "madewhite": "madewhite",
    "melazero": "melazero",
    "melazero v2": "melazero_v2",
    "white tranex complex": "white_tranex_complex",
    "tone up complex": "tone_up_complex",
    "gluta bright complex": "gluta_bright_complex",
    "vitamin c booster complex": "vitamin_c_booster_complex",
    "dark spot corrector complex": "dark_spot_corrector_complex",
    "pore refining complex": "pore_refining_complex",
    "pore minimizing complex": "pore_minimizing_complex",
    "sebum control complex": "sebum_control_complex",
    "oil balancing complex": "oil_balancing_complex",
    "anti shine complex": "anti_shine_complex",
    "blackhead clear complex": "blackhead_clear_complex",
    "clay detox complex": "clay_detox_complex",
    "cica complex": "cica_complex",
    "cica reedle complex": "cica_reedle_complex",
    "centella complex": "centella_complex",
    "centella asiatica 5x": "centella_asiatica_5x",
    "heartleaf complex": "heartleaf_complex",
    "soothing complex": "soothing_complex",
    "anti redness complex": "anti_redness_complex",
    "calming barrier complex": "calming_barrier_complex",
    "acne clear complex": "acne_clear_complex",
    "anti acne complex": "anti_acne_complex",
    "trouble care complex": "trouble_care_complex",
    "spot control complex": "spot_control_complex",
    "blemish control complex": "blemish_control_complex",
    "peptide complex": "peptide_complex",
    "5 peptide complex": "peptide_complex_5",
    "5 peptides complex": "peptide_complex_5",
    "collagen boost complex": "collagen_boost_complex",
    "firming complex": "firming_complex",
    "elasticity complex": "elasticity_complex",
    "retinol booster complex": "retinol_booster_complex",
    "retinal repair complex": "retinal_repair_complex",
    "lifting complex": "lifting_complex",
    "bifida complex": "bifida_complex",
    "galactomyces complex": "galactomyces_complex",
    "fermented yeast complex": "fermented_yeast_complex",
    "probiotic complex": "probiotic_complex",
    "microbiome complex": "microbiome_complex",
    "derma complex": "derma_complex",
    "skin repair complex": "skin_repair_complex",
    "multi care complex": "multi_care_complex",
    "total skin solution complex": "total_skin_solution_complex",
}
# =========================
# 許可タグ定義
# =========================
ALLOWED_TAGS = {
    "cleansing_tags": {
        "makeup_removal",
        "sunscreen_removal",
        "sebum_cleansing",
        "blackhead_prevention",
        "pore_preventive",
        "low_friction",
        "easy_rinse",
        "non_stripping",
        "barrier_preserving",
        "residue_free",
        "heavy_makeup_ok",
        "light_makeup_ok",
        "daily_use_friendly",
        "morning_cleanse_ok",
        "essential_oil",
    },

    "active_ingredients": set(INGREDIENT_TAGS),
    "support_ingredients": set(INGREDIENT_TAGS),

    "formulation": {
        "liposome",
        "encapsulation",
        "capsule",
        "mild_formula",
        "low_ph",
        "barrier_formula",
        "essence_texture",
        "cream_texture",
        "waterproof",
        "tone_up",
        "acid_toner",
        "pad",
        "powder_formula",
        "gel_formula",
        "gommage",
        "luxury_formula",
        "nano_capsule",
        "fermentation",
        "derma_formula",
        "stabilized_vitamin_c",
        "low_irritation",
        "non_comedogenic",
        "oil_formula",
        "balm_formula",
        "milk_formula",
        "water_formula",
        "gel_to_oil_transform",
        "low_friction_system",
        "easy_rinse_system",
        "barrier_preserving",
    },

    "concerns": {
        "pores",
        "texture",
        "clogged_pores",
        "blackheads",
        "acne",
        "breakout",
        "blemish",
        "redness",
        "sensitivity",
        "soothing",
        "heat",
        "irritation",
        "oil",
        "oil_control",
        "sebum",
        "shine",
        "dryness",
        "hydration",
        "moisture",
        "barrier",
        "skin_barrier",
        "repair",
        "dullness",
        "brightening",
        "tone_up",
        "whitening",
        "dark_spot",
        "hyperpigmentation",
        "uv_damage",
        "aging",
        "wrinkle",
        "firmness",
        "elasticity",
        "lifting",
        "uv_protection",
        "sun_protection",
    },

    "skin_types": {
        "dry",
        "oily",
        "mixed",
        "sensitive",
    },

    "availability_japan": {
        "amazon",
        "rakuten",
        "qoo10",
        "store",
        "official_jp",
    },

    "signature_ingredients":{
        "rice_power_no11",
        "rice_power_no6",
        "multi_ceramide_complex",
        "ceramide_complex_ex",
        "derma_barrier_complex",
        "moisture_lock_complex",
        "hyaluronic_5d_complex",
        "aqua_sphere_complex",
        "ectoin_protect_complex",
        "madewhite",
        "melazero",
        "melazero_v2",
        "white_tranex_complex",
        "tone_up_complex",
        "gluta_bright_complex",
        "vitamin_c_booster_complex",
        "dark_spot_corrector_complex",
        "pore_refining_complex",
        "pore_minimizing_complex",
        "sebum_control_complex",
        "oil_balancing_complex",
        "anti_shine_complex",
        "blackhead_clear_complex",
        "clay_detox_complex",
        "cica_complex",
        "cica_reedle_complex",
        "centella_complex",
        "centella_asiatica_5x",
        "heartleaf_complex",
        "soothing_complex",
        "anti_redness_complex",
        "calming_barrier_complex",
        "acne_clear_complex",
        "anti_acne_complex",
        "trouble_care_complex",
        "spot_control_complex",
        "blemish_control_complex",
        "peptide_complex",
        "peptide_complex_5",
        "collagen_boost_complex",
        "firming_complex",
        "elasticity_complex",
        "retinol_booster_complex",
        "retinal_repair_complex",
        "lifting_complex",
        "bifida_complex",
        "galactomyces_complex",
        "fermented_yeast_complex",
        "probiotic_complex",
        "microbiome_complex",
        "derma_complex",
        "skin_repair_complex",
        "multi_care_complex",
        "total_skin_solution_complex",
    },
       

    "main_functions": set(MAIN_FUNCTION_MAP.values()),

    "ingredient_focus": {
        "毛穴ケア",
        "皮脂コントロール",
        "ニキビケア",
        "鎮静ケア",
        "赤みケア",
        "保湿",
        "バリア修復",
        "美白",
        "くすみ改善",
        "エイジングケア",
        "ハリ改善",
        "角質ケア",
        "ターンオーバー促進",
        "紫外線防御",
    },

    "technology": set(technology_labels.keys()),

    "texture": {
        "light",
        "rich",
        "watery",
        "gel",
        "cream",
        "essence",
        "medium",
        "oil",
        "balm",
        "foam",
        "powder",
        "milky",
    },

    "contraindications": {
        "sensitive_skin",
        "retinol_beginner",
        "retinol_same_routine",
        "acid_same_routine",
        "high_irritation_risk",
        "dry_skin_caution",
        "redness_prone",
        "daily_use_caution",
        "morning_use_caution",
        "oily_skin_caution",
        "photosensitivity",
        "retinol_pregnancy",
        "bee_product_allergy",
        "essential_oil_caution",
        "fungal_acne_caution",
        "heavy_makeup_not_enough",
        "rinse_required",
    },
}
print("loaded constants")
print("AI_CATEGORY_MAP" in globals())