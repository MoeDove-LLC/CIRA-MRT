# mrt-cn-routes

從各大公共 MRT route collector（RouteViews、RIPE RIS、PCH）產生
中國相關 ASN 分組的 IPv4 / IPv6 CIDR 聚合列表。

---

## 安裝

```bash
apt install python3 python3-pip
pip3 install -r requirements.txt
```

`requirements.txt` 內含 `pytest`；若你的環境沒有安裝，執行測試前請先：

```bash
pip3 install pytest
```

外部工具（`bgpdump` 等）**並非必要**：預設會自動使用內建的 native struct 解析器
(比 mrtparse 快約 10 倍),裝了 `bgpdump` 則會優先用它。詳見下方「解析器選擇」。

### 效能：解析器選擇

純 Python 的 `mrtparse` 正確但**很慢**：一份完整的 RouteViews RIB 有數千萬筆
entry，mrtparse 單檔可能要 20～60 分鐘。本腳本內建兩個更快的解析器,並用
`--parser` 選擇:

- `auto`（預設）：有 `bgpdump` 就用它,否則用內建的 native 解析器。
- `native`：內建的 struct 二進位解析器,**不需要任何外部工具**,比 mrtparse 快
  約 10 倍。它在解析當下就用整數集合做 target 預過濾,只有含目標 ASN 的 route
  才會建立物件、進入後續比對。這是預設 `auto` 在沒有 bgpdump 時採用的解析器,
  一般情況下已足夠快,建議優先使用。
- `bgpdump`：外部 C 工具,搭配 `grep` 預過濾時最快(見下)。需另外安裝
  `apt install -y bgpdump`。找不到時會自動退回 native。
- `mrtparse`：純 Python,相容性最高(支援 update replay、少見格式),但最慢。

使用 `bgpdump` 時,若系統有 `grep`,腳本會再插一層 C 的 `grep` 預過濾:只把
AS_PATH 含目標 ASN 的行送進 Python(完整 RIB 通常只有約 6% 命中)。native 解析器
則是在 Python 內部做同等的整數預過濾。兩者在語意上都安全:不含任何 group ASN
的 route 本來就不會被命中。

此外,各 group 的 prefix 以 **set 去重**收集:同一個 prefix 在數十個 peer 下會重
複出現,set 讓它只存一份,大幅降低記憶體與最後 `cidr_merge` 的成本。

### 平行處理:多執行緒下載 + 多行程解析(重疊)

下載是 I/O 密集,用**執行緒池**(`--parallel-downloads`,預設 8);解析是 CPU 密集,
受 Python GIL 限制,改用**行程池**(`--parse-workers`,預設 = CPU 核心數)真正吃滿多核。
兩者**重疊執行**:某個檔一下載完就立刻丟進解析行程池,不必等全部下載完才開始解析,
所以下載與解析同時進行。

每個解析行程各自處理一個檔、回傳該檔命中的 prefix 集合,主行程再用 set 合併去重。

**最長優先排程(LPT)**:少數超大檔(RIS 的 bview 可達數百 MB、RouteViews 大表)
若排在後面,會變成「拖尾」——其他核心都跑完了,只剩一兩個核在啃大檔。所以下載完成
後會**先按檔案大小由大到小排序再解析**,讓大檔一開始就佔滿所有核心、小檔填尾巴,
牆鐘時間明顯下降。注意:單一最大檔是無法再切分的(一個檔 = 一個解析行程),所以牆鐘
時間的下限≈「解析最大那個檔所需的時間」。

**PCH 零 HEAD 探測**:PCH 伺服器很慢,舊版對每個 collector 逐一發 HEAD 確認日期,
數百個會拖很久。現在改成直接產生候選 URL(今天→昨天→前天),下載階段自動挑第一個
存在的,探測階段瞬間完成。

全量跑(數百個檔)時,以上讓牆鐘時間接近「max(最大單檔解析時間, 總解析量 ÷ 核心數)
+ 下載時間」。範例:

```bash
# 用 16 條下載、24 個解析行程(依機器調整)
python3 mrt_cn_routes.py --output-dir /www/wwwroot/cira.moedove.com \
  --parallel-downloads 16 --parse-workers 24 --timeout 180 --verbose
```

記憶體會隨 `--parse-workers` 上升(每個行程各持有一份暫存),核心多但記憶體有限時
可把 `--parse-workers` 調小。

注意：`--verbose` 時 `parse` 進度條是以「檔案」為單位更新的，單一大檔跑完前會停
在 0%，這是正常現象，不是當掉；每 50 萬筆會輸出一次 debug log。

### 關於 IPv6：需要雙棧 collector

`route-views2` 是**純 IPv4** collector，用它跑出來的 `_v6.txt` 會是空的，這是正常
的。IPv6 需要從雙棧來源取得，例如 RouteViews 的 `route-views6`、`route-views.linx`、
`route-views.eqix`，以及 RIPE RIS 的 rrc 系列（v4/v6 皆有）。跑完整來源時就會有
IPv6 結果。

---

## 執行

一般執行（輸出到預設目錄）：

```bash
python3 mrt_cn_routes.py --output-dir /www/wwwroot/cira.moedove.com
```

先看看會下載/處理哪些 MRT 檔（不實際下載）：

```bash
python3 mrt_cn_routes.py --dry-run --verbose
```

指定時間點與部分 collector：

```bash
python3 mrt_cn_routes.py \
  --time 2026-07-09T00:00:00Z \
  --sources routeviews,ris \
  --collectors route-views2,rrc00 \
  --max-files-per-source 4
```

### 主要參數

| 參數 | 預設 | 說明 |
| --- | --- | --- |
| `--output-dir` | `/www/wwwroot/cira.moedove.com` | 產出 `{group}_v4.txt` / `_v6.txt` 的目錄 |
| `--cache-dir` | `/var/cache/mrt-cn-routes` | MRT 下載快取（依 source/collector/date 分層） |
| `--sources` | `routeviews,ris,pch` | 啟用的資料來源 |
| `--source-config` | `./mrt_sources.yml` | 來源設定檔；不存在時使用內建預設 |
| `--time` | `latest` | `latest` 或 ISO8601（例：`2026-07-09T00:00:00Z`） |
| `--collectors` | `all` | `all` 或以逗號分隔的 collector 清單 |
| `--max-files-per-source` | `0` | 每個 source 最多下載的 MRT 檔數；`0` = 不限制（全量） |
| `--parallel-downloads` | `8` | 並行下載數（I/O，執行緒池） |
| `--parse-workers` | `0` | 並行解析的行程數（CPU，行程池）；`0` = CPU 核心數 |
| `--timeout` | `120` | 每個請求的逾時秒數（PCH 較慢，逾時可調大） |
| `--parser` | `auto` | 解析器：auto / native / bgpdump / mrtparse（見下） |
| `--dry-run` | off | 只列出將下載/處理的 MRT 檔 |
| `--keep-cache` | off | 處理後保留 MRT 檔 |
| `--verbose` | off | 詳細 log 與進度條 |
| `--fail-fast` | off | 任一 source/collector 失敗即中止（預設為記 warning 後繼續） |

---

## 輸出

每個分組會輸出兩個檔案：`{group_key}_v4.txt` 與 `{group_key}_v6.txt`，header
沿用舊腳本風格：

```
# Group: China Telecom
# Key: chinatelecom
# Generated: 2026-07-09T00:00:00Z
# Total ASNs: 1
# ASN List: 4134
# Details:
#   4134: China Telecom Backbone

1.0.0.0/24
...
```

`HIDDEN_ASNS`（目前為 `146762`）只在 header 中隱藏，仍然會參與匹配與
CN → T1 過濾。

另外會在輸出目錄產生 `summary.json`，包含 `generated_at`、`enabled_sources`、
`processed_files`、`skipped_files`、`warnings`、`per_group_count_v4/v6`、
`total_raw_routes_seen`、`total_matched_routes`、`total_filtered_cn_to_t1` 等
統計。

---

## 設定檔 `mrt_sources.yml`

複製範例並依需求調整：

```bash
cp mrt_sources.example.yml mrt_sources.yml
```

若 `mrt_sources.yml` 不存在，腳本會使用內建預設（結構與
`mrt_sources.example.yml` 相同）。

### 資料來源說明

**RouteViews**：優先用 metadata API 取得每個 collector 的最新 RIB 檔，API 不可用
時退回 archive URL（`/{collector}/bgpdata/{YYYY.MM}/RIBS/rib.{YYYYMMDD}.{HHmm}.bz2`，
2 小時週期）。

**RIPE RIS**：官方 bview 路徑（`/{collector}/{YYYY.MM}/bview.{YYYYMMDD}.{HHmm}.gz`，
8 小時週期）。v4/v6 皆有。

**PCH**：已內建正確的下載路徑，開箱即用。每個 collector 每天各有一份 v4 與 v6 的
完整快照：

```
{base}/IPv4_daily_snapshots/{YYYY}/{MM}/{collector}/{collector}-ipv4_bgp_routes.{YYYY}.{MM}.{DD}.gz
{base}/IPv6_daily_snapshots/{YYYY}/{MM}/{collector}/{collector}-ipv6_bgp_routes.{YYYY}.{MM}.{DD}.gz
```

當 `sources.pch.collectors` 為空清單時，腳本會從當月目錄索引（`index_base_url`）
**自動枚舉全部 collector**（約 300 個），達成全量覆蓋且不需手動維護清單。要限縮
PCH，就在 `collectors` 明確列出你要的 collector 主機名。PCH 每天發佈，「今天」的檔
可能還沒生成，所以會自動往前回退 `date_lookback_days`（預設 2）天。PCH 伺服器較慢
（可能要等約 1 分鐘才開始下載），必要時用 `--timeout` 調大逾時。

### 全量覆蓋與資源用量

隨附的 `mrt_sources.yml` 已列出**全部** RouteViews（56 個）與 RIPE RIS（23 個）
collector，PCH 則自動枚舉全部（約 300 個）。搭配預設的 `--max-files-per-source 0`
（不限制），一次會下載完整資料集以求最準確的分析。

請注意這會下載**大量資料**：RouteViews / RIS 的全表 RIB 每個數百 MB，PCH 有數百個
collector（每個各有 v4/v6）。完整跑一次可能是數十到數百 GB、耗時數小時。若要輕量
執行，請在 yml 裡精簡 collector 清單，或用 `--max-files-per-source N` 設上限。不可
達的檔案會被跳過並記 warning。


## 過濾邏輯

1. **分組匹配**：只要一條 route 的 AS_PATH 中包含該 group 任一目標 ASN（
   AS_SEQUENCE 或 AS_SET 皆算），即成為候選。
2. **CN → T1 相鄰過濾**：若 AS_PATH 的**有序 AS_SEQUENCE** 中，出現
   `CN_PATH_FILTER_ASNS` 的 ASN **緊鄰**其後為 `T1_ASNS` 的 ASN，則丟棄整條
   route。
   - 只檢查有序的 AS_SEQUENCE。AS_SET 內部無順序，因此**不**參與相鄰判斷
     （但仍可用於「是否包含目標 ASN」的匹配）。
   - AS_CONFED_SEQUENCE / AS_CONFED_SET 預設忽略。
   - 存在 AS4_PATH 時，會依 RFC 6793 與 AS_PATH 合併，優先採用 4-byte ASN 結果。
   - AS_PATH 提取只從 BGP Path Attributes 讀取，與 ADD-PATH 無關。

CN → T1 相鄰過濾之所以在 Python 完成，是因為新流程已無 BIRD，無法再用 BIRD
filter 語言表達；改以純 Python 對 AS_SEQUENCE 逐一比對相鄰對。

---

## 排程執行

### cron（每天 03:30 執行）

```cron
30 3 * * * /usr/bin/python3 /opt/mrt-cn-routes/mrt_cn_routes.py --output-dir /www/wwwroot/cira.moedove.com >> /var/log/mrt-cn-routes.log 2>&1
```

### systemd timer

`/etc/systemd/system/mrt-cn-routes.service`：

```ini
[Unit]
Description=Generate China ASN CIDR lists from public MRT data
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/mrt-cn-routes
ExecStart=/usr/bin/python3 /opt/mrt-cn-routes/mrt_cn_routes.py --output-dir /www/wwwroot/cira.moedove.com
```

`/etc/systemd/system/mrt-cn-routes.timer`：

```ini
[Unit]
Description=Run mrt-cn-routes daily

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

啟用：

```bash
systemctl daemon-reload
systemctl enable --now mrt-cn-routes.timer
```

### 搭配 Cloudflare Pages

CI 已把 `dist/` 提交回倉庫,所以直接用 Cloudflare Pages 的 Git 整合即可托管:

1. Cloudflare 控制台 → Workers & Pages → Create → Pages → Connect to Git,選這個
   GitHub 倉庫(CF Pages 的 Git 整合支援 GitHub / GitLab,不支援 Gitee)。
2. 建置設定:Framework preset 選 **None**;**Build command 留空**;
   **Build output directory 填 `dist`**;Production branch 選你的預設分支(如 `main`)。
3. 部署後,檔案就以固定檔名提供:

   ```
   https://<你的專案>.pages.dev/china_all_v4.txt
   https://<你的專案>.pages.dev/chinatelecom_v4.txt
   https://<你的專案>.pages.dev/            # index.html 會列出全部檔案
   ```

之後每次 CI 提交 `dist/`,Cloudflare Pages 會自動重新部署。若要綁自訂網域,在 Pages
專案的 Custom domains 設定即可。

（Gitee 這邊:CI 也會把 `dist/` 提交回 Gitee 倉庫,可搭配「Gitee Pages」服務托管;
Gitee Pages 免費版通常需手動或定時重新部署。)

## 測試

```bash
python3 -m py_compile mrt_cn_routes.py
pytest -q
```

若 `pytest` 尚未安裝：

```bash
pip3 install pytest
```

測試涵蓋 CN → T1 相鄰過濾、AS_PATH 包含判斷、AS_SET 語義、`cidr_merge` 聚合、
IPv4 / IPv6 分離，以及 mrtparse segment 結構的正規化。

---

## 授權 License

本專案以 **MIT License** 釋出,詳見 [`LICENSE`](LICENSE)。可自由使用、修改、散布與
商業使用,只需保留版權與授權聲明。

產出的路由清單(`{group}_v4.txt` / `_v6.txt`)是衍生自公開 MRT route collector
(RouteViews、RIPE RIS、PCH)的事實性資料,各資料來源的使用條款請參閱其官方網站。
