# CF-01 影子精度探針

此獨立實驗只使用合成數字。dbt project 的 model 設為 `ephemeral`；`probe.py` 明確指定 `--run-bigquery` 才會提交 SELECT query job，不讀取或修改任何資料表。此探針尚未接到正式 Report、驗收、發布或本機模式，也不是核准的捨入規則。切勿把此 fixture 當成公司的財務數據。

2026-11-18 在 project `focus-heuristic-438521-v4`、location `US` 執行合成 SELECT：

| 案例 | Python Decimal 測試基準（28 位有效數字） | BigQuery BIGNUMERIC 原始結果 | 實驗結論 |
|---|---|---|---|
| 120 ÷ 80 | `1.5` | `1.5` | 數值相同 |
| 1 ÷ 3 | `0.3333333333333333333333333333` | `0.33333333333333333333333333333333333333` | 原始結果不同；候選 18 位結果皆為 `0.333333333333333333` |
| `1E-39` ÷ 1 | `1E-39` | 輸入轉換後為 `0` | `SAFE_CAST` 未保持輸入精度，探針標記 `blocked_input_precision`，不能接受為有效結果 |
| 0 ÷ 20 | `0` | `0` | 真實零保留為零 |
| 1 ÷ 0 / 缺少分子 | 無值 | 無值 | 分別標記 `zero_denominator`／`missing_input` |
| -50 ÷ 100 / 50 ÷ -100 | `-0.5` | `-0.5` | 測試中兩種負號結果相同 |

另以合成 SQL 測試 `ROUND_HALF_EVEN` 的兩個 18 位 tie 案例：`0.1234567890123456785` 得 `0.123456789012345678`；`0.1234567890123456795` 得 `0.12345678901234568`（末尾零不影響數值）。這些案例尚不足以證明其他輸入範圍、double rounding 或歷史財務資料的等價性。

重現（會建立 BigQuery 計費 query job；需要已授權的 project 與 credentials）：

```sh
venv/bin/python experiments/cf01_shadow/probe.py --run-bigquery --project YOUR_PROJECT --location US
```

只做 dbt parser 檢查（不提交計算查詢）：

```sh
venv/bin/python venv/bin/dbt parse --project-dir experiments/cf01_shadow --profiles-dir references/dbt --target-path /tmp/cf01-shadow-dbt-target --log-path /tmp/cf01-shadow-dbt-logs
```

Python 基準在本實驗明確設為 `prec=28, ROUND_HALF_EVEN`，不得解讀成已驗證所有正式執行環境均使用此 context。候選 18 位規則、允許值域、組織資料治理、歷史結果影響和正式切換仍需確認；遇到轉換失真、溢位或比對不一致必須阻擋，而非以零或 Python 結果補值。
