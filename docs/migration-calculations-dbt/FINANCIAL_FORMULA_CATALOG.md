# Financial Formula Catalog（審閱草案）

> [VERIFIED] 基準 commit `a324acce10e10de902ffe1dd1e128963cb7017fc`。本目錄依 `scripts/loan_review_contract.py:61-110,322-461,550-635` 與 `scripts/loan_review_pipeline.py:165-247,318-348` 靜態整理；未執行測試或查詢 BigQuery。此處「公式已實作」不表示特定公司的定義或資料已取得核准。

## 共通現況與限制

- [VERIFIED] `Fact` 以 `id/metric/period_role/status/value/unit/definition/start/end/entity/scope/evidence_ids/formula_id/formula_version/input_ids/reason/basis` 等欄位追蹤；`Report` 保存 currency、scale、periods、evidence、decisions（`loan_review_contract.py:166-253`）。輸入或結果狀態有 `sourced/calculated/missing/conflict/not_applicable/not_calculable`；不可用 Fact 的 `value` 必須為 null、保留原因且不能附已完成公式資訊（`:34-45,550-570`）。
- [VERIFIED] `calculate_report()` → `calculate_fact()` → `approved_formula_value()`；Report 的角色、entity、scope、輸入順序、period、unit、evidence 與決策由 `calculate_fact` 和 `validate_report` 共同檢查。已可用的 source/calculated 值不覆寫，conflict/not_applicable 不被二次計算洗掉。公式缺輸入或決策時為 `not_calculable`，分母為零回傳 None、reason 為 zero divisor or nonnumeric input。真正的零分子仍可得零（`loan_review_contract.py:322-411`、`loan_review_pipeline.py:205-245`）。
- [VERIFIED] 金額正規化與 SEC 候選版本選擇先於公式（`loan_review_sources.py:49-57,253-347`）。沒有自動幣別轉換。Fact 的值是 `Decimal`，比率以未乘 100 的小數儲存，`current_ratio/quick_ratio/cash_flow_ratio/revenue_growth` 的 contract unit 是 `percent`；interest coverage 是 `multiple`，CF-16 是 `percentage_points`，顯示時才轉換。**不要只按標籤推測倍數或百分比顯示**。
- [VERIFIED] 一般公式執行的版本常數是 `FORMULA_VERSION='acceptance-v1.0-addendum-2026-10-23'`（`loan_review_pipeline.py:32,243-244`），但 `calculate_fact()` 的 public interface 允許呼叫者傳入其他非空 version；測試的 `'1.0'` 不是正式 workflow 版本。DER 由 `calculate_identity()` 呼叫者提供非空 `formula_version`，沒有在該函式中固定版本。
- [VERIFIED] 除借款加總特設較高 local context 外，`approved_formula_value` 的加減／除法使用呼叫時 Decimal context，沒有在各公式中指定固定捨入位數（`loan_review_contract.py:322-353`）。`normalize_value` 使用較高精度及 `Inexact` 保護（`loan_review_sources.py:49-57`）。[UNVERIFIED] 執行環境是否自訂 Decimal context、BigQuery 查詢結果如何與該 context 完全一致，均須實測；`BIGNUMERIC` 有有限範圍與小數位，不能聲稱自動等價。精度決策前禁止正式切換。
- [PROPOSED] 共同規劃：Python 先審查 eligibility 與建立有序 inputs、日期及 evidence；dbt 只計算批准的 cloud 指標；Python 對結果進行不回填的獨立驗證、套入既有 Fact，並執行驗收。local 模式對遷移後指標明確標示不可提供，不以 Python 靜默補算。
- [PROPOSED] 每個公式均需 golden fixtures、資料來源／輸入 ID 可追溯、zero/missing/conflict、scope/entity、as_of、input order、annual/non-calendar/YTD、signed values、Decimal/BIGNUMERIC 邊界、失敗後不發布、歷史輸出及 Excel 一致性測試；以下另列每個公式特別需要的測試。

## Group A：DER-01 至 DER-06

[VERIFIED] 共同條件：`calculate_report(identity_reviews=())` 只對呼叫者明確給定的 `identity_reviews` 呼叫 `calculate_identity`；`report_from_source_bundle()`、一般 `run_review()` 路徑未提供此參數，因此目前不會自動執行 DER（`loan_review_pipeline.py:165-169,549-559`；`review_workflow.py:185-188`）。各 DER 的 target 須存在且不可覆蓋可用值；需同角色、相同 start/end、unit 與 currency 的兩個有序 input，和同期間 `kind=definition` evidence、非空 formula_version；拒絕循環／反向推導與不可用輸入（`loan_review_contract.py:413-461`）。entity/scope 由 `validate_report` 驗證。definition evidence 必須真實涵蓋同一 reporting scope、tax、discontinued operations、NCI 等橋接項；單純數字相等或 tag 名稱不能證明（同一函式 docstring）。

[VERIFIED] 所有 DER 結果為 `money`、duration、report currency/scale，會在成功時儲存 definition evidence 與 input IDs；上游可為 sourced 或 calculated。正負號按輸入值原樣運算：特別是所得稅利益可為負數，不做絕對值或憑空反轉（`METRICS` 與 `calculate_identity`）。`validate_arithmetic` 對 DER 以同一規則表重算；render、JSON、Excel 與 cloud fact/evidence 視圖消費同一 Report（`loan_review_pipeline.py:318-348,613-646`; `export_loan_review_excel.mjs:expression`; `review_cloud.py:publish`）。

### DER-01 — Gross Profit

- [VERIFIED] `gross_profit = revenue - cost_of_revenue`；有序輸入 `(revenue, cost_of_revenue)`，兩者均為目標同期間 duration。來源實作 `loan_review_contract.py:423,455`；可用於上游 `CF-10 gross_margin` 與後續 DER-03/04 的已核准 input。
- [VERIFIED] definition evidence 必須證明收入與成本在同 scope 下構成完整 gross-profit identity。`tests/test_loan_review_calculations.py` 有 DER-05 identity 關卡範例；[UNVERIFIED] 尚未找到此 ID 專屬完整 e2e 測試。
- [PROPOSED] dbt 僅在 explicit reviewed eligibility 下計算；需測收入／成本口徑差、負數、已直接列報毛利不可覆寫、DER-01→DER-02 循環與精度界限。

### DER-02 — Cost of Revenue

- [VERIFIED] `cost_of_revenue = revenue - gross_profit`；有序輸入 `(revenue, gross_profit)`（`loan_review_contract.py:424,455`）。來源毛利和衍生毛利都可為候選，但後者若依賴目標成本，循環檢查拒絕。下游可供 DER-01，但禁止反向循環。
- [PROPOSED] 雲端 dbt 條件同 Group A；測不同 source scope、毛利依賴成本時阻擋、直接成本保留與 signed 數字。 [UNVERIFIED] 未確認此 ID 個別 e2e 已覆蓋。

### DER-03 — Operating Expenses

- [VERIFIED] `operating_expense = gross_profit - operating_income`；輸入 `(gross_profit, operating_income)`（`loan_review_contract.py:425,455`）。費用定義是排除 cost of revenue 且 scope 經核實（`:64-66`）；可供 DER-04，但不可循環。
- [PROPOSED] 雲端 dbt 條件同 Group A；測其他營業項目／分類的定義證據不足時阻擋、循環與符號。[UNVERIFIED] 未確認此 ID 個別 e2e 已覆蓋。

### DER-04 — Operating Income

- [VERIFIED] `operating_income = gross_profit - operating_expense`；輸入 `(gross_profit, operating_expense)`（`loan_review_contract.py:426,455`）。可能供 CF-11 operating_margin，再供 CF-16；同樣不得覆寫 source。
- [PROPOSED] 雲端 dbt 條件同 Group A；測非營業項目分類、缺 evidence、DER-03／04 反推循環及 CF-16 依賴。[UNVERIFIED] 未確認此 ID 個別 e2e 已覆蓋。

### DER-05 — Pretax Profit

- [VERIFIED] `pretax_profit = net_income + income_tax`；輸入 `(net_income, income_tax)`（`loan_review_contract.py:427,455`）。tax benefit 應保留負號；可能供 CF-04、CF-12，但不能忽視 NCI、停業部門、所得稅及合併範圍。`tests/test_loan_review_calculations.py` 明確測 definition evidence 與此 identity。
- [PROPOSED] 雲端 dbt 條件同 Group A；加測負稅額、NCI／discontinued scope、evidence 不綁定目標角色、下游 CF-04。不得把數值相等當作核准。

### DER-06 — Net Income

- [VERIFIED] `net_income = pretax_profit - income_tax`；輸入 `(pretax_profit, income_tax)`（`loan_review_contract.py:428,455`）。保留負所得稅；可能供正式 Standard EBITDA（目前不在遷移範圍）。
- [PROPOSED] 雲端 dbt 條件同 Group A；測負稅額、NCI／discontinued scope、DER-05/06 反向循環。[UNVERIFIED] 未確認此 ID 個別 e2e 已覆蓋。

## Group B：CF-01、02、03、04、09、16

### CF-01 — Current Ratio／流動比率

- [VERIFIED] `current_ratio = current_assets / current_liabilities`，ordered inputs `(current_assets, current_liabilities)`；兩者為同報告角色期末 instant。`METRICS['current_ratio']` 無額外 pending decision；兩個 input 各須可用且有對應來源／衍生 lineage（`loan_review_contract.py:77,322-411,602-635`）。
- [VERIFIED] 合約值為 fraction、unit=`percent`：例如 120/80=1.5，以原合約百分比顯示可能為 150%，而 1.5x 是另一表達；商業公式頁若顯示 1.50x 必須明示等值並保持既有輸出語意。`tests/test_loan_review_calculations.py` 有零分子、零分母與 Fact lineage 測試；`test_loan_review_pipeline.py` 有年度／季度案例。
- [PROPOSED] 第一個 cloud dbt vertical slice。測跨 entity/run 汙染、負／零分母、精度與 local 不提供。來源（含 SEC tag、period、version）由 input fact → evidence 回溯。

### CF-02 — Quick Ratio／速動比率

- [VERIFIED] `quick_ratio = quick_assets / current_liabilities`；有序輸入均為同角色期末 instant，結果為 fraction、unit=`percent`。需要 `DEC-01`，quick_assets 亦受同一決策約束（`loan_review_contract.py:78,97-100,373`）。來源不足不可以 current assets 代替 quick assets。`tests/test_loan_review_calculations.py` 測缺 decision。
- [PROPOSED] 與 CF-01 共用 dbt interface，但保留個別 eligibility；測未批准 DEC-01、有／無速動資產證據、來源衝突、零分母。legacy `int_liquidity_ratios.sql` 使用 current assets 近似，明確不等價。

### CF-03 — Cash Flow Ratio／現金流量比率

- [VERIFIED] `cash_flow_ratio = cfo / current_liabilities`，順序 `(cfo,current_liabilities)`；CFO 是目標角色 fiscal annual／YTD duration，流動負債是同一角色的期末 instant（`loan_review_contract.py:72,79,276-283,622-635`）。值是 fraction、unit=`percent`；沒有額外 pending decision；來源必須有可用值。
- [PROPOSED] 比對 YTD/annual 與同 end date 的獨立季度，禁止誤用 TTM 或先前期末負債；測正負 CFO、零負債、日期角色及金額單位。[UNVERIFIED] 此公式逐項 e2e 覆蓋尚未完整驗證；legacy 使用 `ttm_cfo_m`，期間不等價。

### CF-04 — Interest Coverage／利息保障倍數

- [VERIFIED] `(pretax_profit + interest_expense) / (interest_expense + capitalized_interest)`，順序 `(pretax_profit,interest_expense,capitalized_interest)`；同角色 duration，unit=`multiple`。需要 `DEC-05`；interest expense 定義為未與 interest income 抵銷的 gross expensed interest，並排除單列的 capitalized interest（`loan_review_contract.py:67,81,98-103,343-345`）。分母為 0 時不可計算；不自動補 0。
- [VERIFIED] `tests/test_loan_review_calculations.py` 有公式值與 Fact 測試；legacy `int_coverage_ratios.sql` 的 EBITDA/interest 並非相同指標。
- [PROPOSED] 測核准／未核准 DEC-05、利息淨額與資本化利息重複計入、負值／零分母、DER-05 作上游，以及 source proof。

### CF-09 — Revenue Growth／營收成長率

- [VERIFIED] `(current_revenue - prior_revenue) / prior_revenue`；順序 `(當期 revenue, 前期 revenue)`，unit=`percent`。年度相鄰四個 fiscal roles 中前一個；季度只對 `current_interim` 配 `prior_interim` 的可比較 YTD；最早年度或其他季度角色前期不存在時不可計算（`loan_review_pipeline.py:228-244`；`loan_review_contract.py:608-635`）。
- [VERIFIED] 分母可為負，只有 0 不可除；`tests/test_loan_review_calculations.py` 明測 `(-50 - -100)/-100 = -0.5`。這與 `ratio_commentary_analysis._change` 只在前值 > 0 時計算 growth 的分析規則不同，不能共用而改變意義。
- [PROPOSED] Python 明確綁定 period roles；dbt 不用 `LAG` 自行猜前期。測不規則 fiscal calendar、同日期 QTD vs YTD、負前期、零與第一年度缺前期。[UNVERIFIED] 分別對每種 negative/quarter 情況的端到端覆蓋須補足。

### CF-16 — Operating Margin Change／營業利益率變動

- [VERIFIED] `current_operating_margin - prior_operating_margin`，順序 `(當期,前期)`；每個 operating margin 上游為 CF-11 `operating_income / revenue`。`calculate_report` 排序讓 CF-16 在一般 formula 後計算（`loan_review_pipeline.py:202-205,228-244`；`loan_review_contract.py:92,94,341-342`）。同 CF-09 的年度相鄰／季度可比較 YTD 角色規則。結果 unit=`percentage_points`，值為小數差，顯示時乘 100 個百分點。
- [VERIFIED] `tests/test_loan_review_calculations.py` 驗證 `.2 - .3 = -.1`；`build_narratives()` 亦有顯示用直接百分點運算。CF-11 並非初始六項之一，若不一併遷移，上游仍由現有 Python 正式計算，須在介面標記來源與版本。
- [PROPOSED] dbt 接收 Python 已驗證的兩個 CF-11 Fact；測任一營收為 0、CF-11 未提供、方向及 p.p. 顯示。若 DER-04 移轉，需依照拓樸分批回填，不可跨過 eligibility 或假設所有 DER 都預設啟用。

## 其他現存運算與消費者

[VERIFIED] `approved_formula_value()` 亦支援 CF-05、CF-06、CF-10/11/12、COMMON-SIZE-v1、CASH-CHANGE-v1、LEVERAGE-v1、STANDARD-EBITDA-v1、TOTAL-DEBT-v1、CURRENT-DEBT-v1；DSCR/CFADS 定義有待確認，並無通用已批准公式（`loan_review_contract.py:76-94,322-353`）。`calculate_report` 的 total-debt 路徑一致性檢查直接呼叫公式；`validate_arithmetic` 重算所有 calculated facts。`ratio_commentary_analysis.py` 的比較及勾稽是分析輸入，`export_loan_review_excel.mjs` 獨立建立 Excel 公式，`references/dbt/models/model/intermediate/*` 是 legacy KPI。這些功能不因本計畫候選名單而自動遷移。

[PROPOSED] 每個目標的下游保留 Report schema、Narrative fact_ids、JSON/Markdown/HTML/Excel、`raw_review_results` 和 Looker 讀取方式；增加商業公式說明時以 formula_id + version 對應受控 registry，並測 registry、SQL、Python verifier、Excel 公式的 fixture 結果與輸入順序一致。
