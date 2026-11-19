# Phase 3–5 實作狀態

2026-11-18：已加入 CF-02/03/04/09/16 與 DER-01..06 的逐公式 dbt 計算、shadow 比對、四位小數 HALF_EVEN（DER 為精確加減）、離線 unavailable、審核證據與公式 metadata。正式 workflow 從 shadow 切換時要求相同公司及 scope 的該公式成功紀錄，核對內容摘要、實際 BigQuery job SQL 與參數；各 DER 仍須公司／期間的明確 identity review。shadow 至少一筆匹配且無差異並不代表完整驗收。

驗證：308 個離線測試通過（7 項選擇性測試跳過）；新增 shadow job 防竄改測試與相關測試共 21 項通過；BigQuery 合成 SELECT 驗證 5 項 CF、6 項 DER、精度損失拒絕、job 核對，以及 5 項 CF 經報告與 Excel 的流程通過。測試沒有發布正式資料或建立正式 view。

尚未完成正式 rollout 驗收：真實公司與期間的 shadow／人工審閱、DER definition evidence、歷史影響及併發／完整失敗案例、正式發布與 Looker 實測。選取開關不等同這些驗收已完成；保留逐公式回退與既有發布關卡。BigQuery job 不再可讀時會阻擋後續切換。

2026-11-19 更新：依使用者新決定，`--dbt-migrate-all` 對 DER-01..06 不再要求逐公司人工審核或 DER shadow，輸入可用時由 dbt 自動推導。報告與 metadata 明示尚未審核公司特定定義；來源直接列報優先，期間、單位、衝突與循環檢查仍適用。CF-02/03/04/09/16 仍要求各自成功 shadow。此更新取代上方 2026-11-18 所述 DER 審核前提。
