# Context Map：財務公式遷移前的程式盤點（審閱草案）

> [VERIFIED] 盤點基準：2026-11-17，branch `codex/migrate-calculations-to-dbt`，HEAD `a324acce10e10de902ffe1dd1e128963cb7017fc`。盤點前 working tree 有未追蹤的 `docs/data-flow/`，本草案不覆寫它。本次未執行 pipeline、測試、dbt 或雲端查詢。以下路徑相對 repository root，行號是此基準的定位提示。
>
> [PROPOSED] 此文件是供使用者審閱的規劃，不代表已批准或完成遷移。公式細目見 `FINANCIAL_FORMULA_CATALOG.md`，切換和 rollback 見 `MIGRATION_PLAN.md`。

## Overview

- [VERIFIED] 目前正式 Loan Review 以 `scripts/review_workflow.py:run_review` 協調來源、Python 計算、報表、可選 cloud 發布；`scripts/agent_runner.py` 是 CLI/HTTP 入口。`references/loan-review-integration.md` 是現有整合說明，應在未來實作並驗證後更新，不應將草案當作既有行為。
- [VERIFIED] 原始輸入包括 SEC、snapshot、BigQuery 舊 ledger／舊 run bundle、Nexstar 專用 workbook；前三者以 `scripts/agent_runner.py:prepare_loan_review_source_stage`、`scripts/loan_review_sources.py:prepare_source_inputs` 建立 bundle，workbook 以 `scripts/nexstar_workbook_review.py:build_inputs` 提供 `base_report`。來源不同但共用正式 Report 管線（`scripts/review_workflow.py:run_review`）。
- [VERIFIED] 既有說明 `references/loan-review-integration.md` 的 Excel 段稱 Excel 出錯不阻擋 cloud 發布，但同文件 cloud sequence 與 `scripts/review_workflow.py:280-292` 的 `manifest.get('errors')` 均顯示要求的輸出失敗會阻擋。未來應修正原文件；此處不修改它。

## Current Architecture

[VERIFIED] 以下為 SEC + cloud 的實際連線；local 模式跳過雲端寫入與 dbt，但 `source=bigquery` 仍可使用 BigQuery 作來源。snapshot 不做 SEC 擷取；workbook 走另一 adapter。

```text
agent_runner.py CLI / HTTP
 → review_workflow.run_review
 → agent_runner.prepare_loan_review_source_stage
 → sec_client.SECEdgarClient.extract_financial_facts
 → loan_review_sources.prepare_source_inputs（request、as_of、候選版本、期間、單位）
 → 本機 source_inputs.json
 → review_cloud.ReviewCloud.stage_bundle [cloud]
 → BigQuery raw_review_sources（metadata / inputs / evidence）
 → dbt stg_loan_review_run_sources（view）→ ReviewCloud.read_bundle → 完整 JSON round-trip 比對
 → loan_review_pipeline.report_from_source_bundle / complete_report
 → apply_approved_policy → adopt_inputs → calculate_report → calculate_fact
 → loan_review_contract.approved_formula_value → Report facts
 → build_narratives → acceptance_result／verify_reconciliation／validate_arithmetic
 → formal_commentary + ratio_commentary_analysis + mda_commentary [可選]
 → write_report_artifacts → credit_memo_writer.render_loan_review；review_outputs；Excel exporter
 → ReviewCloud.publish [僅 accepted + 所有要求輸出成功]
 → raw_review_results → dbt formal_review views → raw_review_runs／Looker views
```

[VERIFIED] `scripts/review_cloud.py:58-87` 寫後透過 view 依 `run_id`、`entity` 回讀，且拒絕未知 `record_kind`；`stage_bundle` 比較整個 bundle。`raw_review_sources` 的 schema 是 `run_id, entity, record_kind, record_id, payload, created_at`，payload 是 JSON 字串（`review_cloud.py:29-67`）。`stg_loan_review_run_sources.sql` 的去重 partition 為 `run_id, record_kind, record_id`，不含 entity；run_id 是 workflow 生成的 UUID，任何新設計仍須明確測試 tenant/entity 隔離。

[VERIFIED] `scripts/review_workflow.py:180-188` 在 staging 後呼叫計算；`scripts/review_workflow.py:200-255` 對 SEC 使用 `formal_commentary.collect_sec_documents`、`ratio_commentary_analysis.build_ratio_analysis`、`mda_commentary.generate_formal_commentary` 與 `formal_commentary.apply_commentary`，文字階段失敗記錄 warning；`write_report_artifacts` 重新驗算並產生 acceptance。`review_workflow.py:280-315` 擋住未驗收或輸出錯誤的發布；`ReviewCloud.publish` 再驗證後寫 `raw_review_results`、run/test `tag:formal_review_marts`。dbt 測試或最後 run manifest 失敗不應呈現成功發布（`fct_loan_review_acceptance.sql`）。

## Formal vs Legacy Workflow

- [VERIFIED] Formal Python 計算寫入 `Report`；`references/dbt/models/formal_review/fct_loan_review_facts.sql` 從 `raw_review_results` 提取結果與 `value_exact`，`numeric_value` 是 `SAFE_CAST(... AS BIGNUMERIC)`。`vw_looker_review_facts.sql`、`vw_looker_review_runs.sql` 服務已接受、已發布 run；沒有 formal dbt 計算模型。
- [VERIFIED] Legacy 使用 `references/dbt/models/model/{staging,intermediate,marts}`，`int_liquidity_ratios.sql` 把 current assets/current liabilities 作為 quick ratio 近似；`int_coverage_ratios.sql` 的 interest coverage 是 TTM EBITDA/interest，而 formal CF-04 是稅前利益及利息的另一公式。`scripts/agent_runner.py:_enrich_rows`、`query_legacy_mart` 與 `scripts/credit_memo_writer.py` 亦有相容計算。相同名稱不代表語意相同。
- [UNVERIFIED] 使用者目前實際生產使用哪個 legacy HTTP、批次、外部 dbt 或 Looker consumer；無法從 repository 搜尋證明外部使用不存在。

## Financial Calculation Call Graph

```text
complete_report(report, inputs, candidates, identity_reviews=())
 ├─ apply_approved_policy → adopt_inputs
 ├─ calculate_report(identity_reviews)
 │   ├─ calculate_identity(...)       # 只有明確傳入 identity_reviews 才呼叫
 │   ├─ approved_formula_value(TOTAL-DEBT-v1, …)  # 僅檢查借款路徑一致性
 │   └─ calculate_fact(...) → approved_formula_value(...)
 ├─ build_narratives（年度/季度比較及百分點）
 └─ acceptance_result → validate_arithmetic + verify_reconciliation
```

[VERIFIED] 呼叫點見 `scripts/loan_review_pipeline.py:165-247,318-348,516-559`；`calculate_identity` 直接執行 `items[0].value + sign * items[1].value`，不經 `approved_formula_value`（`loan_review_contract.py:413-461`）。正式 `report_from_source_bundle` 未傳 `identity_reviews`，預設路由不會自動推導六種 identity。`calculate_report` 對 CF-16 先算其他一般公式，確保 operating margin 先存在。

## Calculation Dependency Map

[VERIFIED] 總覽；順序、逐項限制與測試缺口見公式目錄。

| ID | 有序輸入 | 期間與附註 |
|---|---|---|
| DER-01 / 02 | revenue、cost_of_revenue / revenue、gross_profit | 同角色、相同日期及單位；覆核定義 |
| DER-03 / 04 | gross_profit、operating_income / gross_profit、operating_expense | 同上；gross_profit/operating_income 本身可為經許可衍生 |
| DER-05 / 06 | net_income、income_tax / pretax_profit、income_tax | 同上；所得稅保留正負號 |
| CF-01 / 02 | current_assets、current_liabilities / quick_assets、current_liabilities | 同一時點；CF-02 需 DEC-01 |
| CF-03 | cfo、current_liabilities | 同 role：CFO duration/YTD，負債為期末 instant |
| CF-04 | pretax_profit、interest_expense、capitalized_interest | 同 duration；需 DEC-05 |
| CF-09 | revenue（本期）、revenue（前期） | 年度前一年；季度 current_interim 對 prior_interim YTD |
| CF-16 | operating_margin（本期）、operating_margin（前期） | 上游 CF-11=operating_income/revenue；同 CF-09 的配對 |

## Python/dbt Responsibility Boundaries

- [PROPOSED] Python 擁有抽取、候選選擇、資料身分與日期校驗、可計算資格、決策／證據檢查、period matching、Report 還原、獨立驗證、勾稽、敘述、驗收與發布。dbt 僅對明確核准的、已具體綁定 input IDs 的候選執行公式；不自行推定 period、來源或 business approval。
- [PROPOSED] 建立單一 computation adapter interface：request 含 run/entity/target fact、formula/version、有序 input IDs/值/日期、approval/evidence 與 eligibility；response 必須逐目標完整回傳 result 或具體 failure，Python 驗證後寫回既有 Fact。local Python adapter、cloud dbt adapter 在此 seam 變動；正式 cloud authority 為 dbt，Python 的獨立運算僅驗證，不可補值。
- [UNVERIFIED] 欄位、暫存方式及數值合約須先經實作原型與使用者審閱；`source_inputs.json` 並不等於已採用且已通過政策檢查的 Report。

## Shared Assets and Blast Radius

[VERIFIED] 以下分類根據 repository `rg` 的直接引用／呼叫，外部消費者仍未知；不因名稱推論為專用。

| 資產 | 分類 | 已查到的直接與間接影響 |
|---|---|---|
| `loan_review_contract.py`：`Fact`、`validate_report`、計算函式 | [SHARED] | 正式 run、`nexstar_workbook_review.py`、`agent_runner.py` 低階正式支線及多個 `tests/test_loan_review_*`；無證據顯示 legacy KPI 使用同一 contract |
| `loan_review_pipeline.py`：`complete_report`、`calculate_report` | [SHARED] | 正式 SEC/snapshot/BQ、Nexstar adapter、低階 `agent_runner.analyze_ticker` formal-report 分支；測試直接呼叫 |
| `loan_review_sources.py`：`normalize_value`、來源選擇 | [SHARED] | formal 來源及 `ratio_commentary_analysis.py` 等；來源處理保留 Python |
| `review_workflow.py`、`review_cloud.py` | [FORMAL_ONLY] | CLI/HTTP 正式流程、workbook、cloud mock tests；`raw_review_sources`/`results` 的既有 reader/views 需維持相容 |
| `export_loan_review_excel.mjs:expression` | [FORMAL_ONLY] | 將同樣 formula_id 翻成 Excel 計算式並對照 Report，不能只改 Python 與 dbt；Excel 使用 JS number，精度另限 |
| `credit_memo_writer.py`、`agent_runner.py` | [SHARED] | 前者同時有 `render_loan_review` 與 legacy `generate_credit_memo`；後者同時是 formal 入口與 legacy 相容流程 |
| `references/dbt/models/formal_review/*` | [FORMAL_ONLY] | facts、evidence bridge、acceptance 與 Looker views；輸出 JSON payload 現已保留 Fact 的公式與 input IDs |
| `references/dbt/models/model/*`、legacy raw tables | [LEGACY_ONLY] | 現存 legacy KPI/SQL，與 formal 計算不應共用近似定義 |
| 外部工作排程、其他 repositories、BI consumers、真實 BigQuery 資料 | [UNVERIFIED] | 未查雲端與其他 repository；切換前需要盤點擁有者與消費者 |

## Existing Financial Verification Controls

[VERIFIED] `loan_review_sources.py:parse_request/validate_sec_rows/prepare_source_inputs` 限制 entity/CIK、`as_of`、來源版本、scope、期間與單位；`DEC-07` 只對明確 amendment 同概念選最新版，其他衝突保留。`loan_review_contract.py:calculate_fact/calculate_identity/validate_report` 要求狀態、公式、input order、期間、定義及 evidence，禁止循環、未知當零與覆寫既有可用值。

[VERIFIED] `loan_review_pipeline.py:validate_arithmetic/verify_reconciliation/acceptance_result` 重算、驗證來源與勾稽、精度和最終狀態。`review_cloud.py:publish` 再檢驗，`fct_loan_review_acceptance.sql` 結合 run manifest 的 accepted/published；技術通過不等於業務驗收。`formal_commentary.py` 與 `mda_commentary.py` 處理敘述而不替代公式。

## Tests and Repository Conventions

- [VERIFIED] 測試為 `unittest`，例如 `tests/test_loan_review_calculations.py` 覆蓋 CF-01 零分母、CF-04、CF-09 負前期、CF-16 與 DER-05 證據；並非 12 個候選公式皆有逐一端到端覆蓋。
- [VERIFIED] `tests/test_loan_review_sources.py` 覆蓋 entity、as_of、修訂、單位與精度；`test_loan_review_pipeline.py` 覆蓋 annual/YTD/缺值與防竄改；`test_pr_review_regressions.py` 覆蓋勾稽、cloud gate 和 legacy；`test_review_integration.py` 以 mock 測試 round-trip/dbt fail；`test_review_excel_export.py` 驗證 Excel。formal dbt 有 `schema.yml` 及 `references/dbt/tests/*`；非真實 BigQuery parity 證明。
- [VERIFIED] `references/dbt/dbt_project.yml` / `profiles.yml` 配置 BigQuery、環境變數與 legacy 物化設定。`scripts/requirements-workbook-review.txt` 僅明列可選 `xlrd==2.0.2`；未找到 root requirements/pyproject 鎖定所有核心依賴。程式 import 包含 `requests`、Google Cloud BigQuery、`anthropic`；Excel 使用 bundled Node 和 `@oai/artifact-tool`（`review_workflow.py:export_excel`）。
- [UNVERIFIED] 本次未執行任何測試、dbt parse、live SEC／BigQuery；不聲稱測試通過或環境 credentials 可用。

## Staleness Rules

[PROPOSED] 若 HEAD、formula ID/版本、`METRICS`、狀態、source adapter、dbt model、Report contract、驗收條件或輸出格式改變，重新追蹤本圖與目錄；每次更新保留 commit、檢查日期、受影響 tests 和未驗證項。實作驗證後，將結論回寫 `references/loan-review-integration.md`、`references/loan-review-calculation-implementation.md`、必要時 `references/output-format.md`，避免平行文件互相矛盾。
