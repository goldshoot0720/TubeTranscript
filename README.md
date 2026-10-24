# TubeTranscript

貼上 YouTube 網址，用 AssemblyAI 產生**中英／中日雙語動態字幕**：播放時原文逐字高亮、中文同步顯示，並可匯出 SRT / ASS / VTT / TXT。

```
YouTube 網址 → yt-dlp 抽音訊 → AssemblyAI 轉錄（詞級時間戳）
            → 切句 → 整句翻譯 → 依時間分配 → 播放器 + 字幕檔
```

## 快速開始

```bash
./run.sh
```

會自動開啟 <http://127.0.0.1:8765>。

第一次使用需要：

1. 到 [AssemblyAI](https://www.assemblyai.com/dashboard/signup) 註冊，取得 API Key（有免費額度）。
2. 在本機安裝 `yt-dlp` 與 `ffmpeg`：

```bash
brew install yt-dlp ffmpeg
```

## 使用方式

1. 貼上 YouTube 網址（支援 `watch?v=`、`youtu.be`、`/shorts/`、直播存檔）。
2. 貼上 AssemblyAI API Key（可勾「記住」，只存在瀏覽器 localStorage）。
3. 選擇來源語言（自動偵測／英文／日文）與中文字幕（繁／簡）。
4. 按「產生字幕」。

處理完成後：

- **播放器**：YouTube 內嵌播放，原文逐字高亮，中文在下方同步顯示。可切換「雙語／原文／譯文」、調整字級與延遲（±1.5 秒）、全螢幕。
- **字幕列表**：點任一時間碼即可跳轉，可搜尋。
- **匯出**：SRT（雙語／原文／譯文）、ASS（雙語，含樣式，可直接進剪輯軟體）、VTT、TXT。
- **最近紀錄**：結果存在 `jobs/`，關掉瀏覽器再回來仍可開啟（不會重複扣 AssemblyAI 額度）。

## 設計重點

| 項目 | 做法 |
|---|---|
| 音訊 | yt-dlp 取最佳音軌 → ffmpeg 轉 16kHz 單聲道 mp3，上傳後立即刪除 |
| 轉錄 | AssemblyAI `/v2/transcript`，取 `words` 詞級時間戳；未指定語言時自動偵測 |
| 切句 | 先依句末標點切句；過長句子依逗號切成子句，最後才硬切（英 84 字／日 42 字上限） |
| 翻譯 | 以**整句**為單位翻譯（避免半句失去上下文），同一句被拆成多個字幕時，依時間比例分配譯文並盡量切在標點 |
| 隱私 | API Key 只存在瀏覽器與該次請求的記憶體，不寫入伺服器檔案；服務只綁 127.0.0.1 |

## 疑難排解

**「YouTube 要求登入驗證」**
YouTube 對部分 IP 會要求登入。在「進階選項」選擇你的瀏覽器（讀取 cookies），或先更新 yt-dlp：

```bash
yt-dlp -U
```

**「API Key 無效」**
按輸入框下方的「驗證 Key」確認。Key 可在 AssemblyAI Dashboard 重新產生。

**翻譯看起來怪**
翻譯使用 Google 翻譯的免費端點，逐句處理、沒有全文上下文，專有名詞或雙關語可能失準。可先匯出 SRT 再人工校對。

**影片無法內嵌播放**
少數影片關閉了嵌入權限，播放器會顯示提示；字幕列表與匯出功能仍可正常使用。

## 檔案結構

```
app.py          FastAPI 服務：任務管理、yt-dlp、AssemblyAI、匯出
subtitles.py    切句、整句翻譯與時間分配、SRT/ASS/VTT/TXT 產生
static/         前端（index.html / style.css / app.js）
jobs/           每筆結果的 result.json（最近 20 筆）
run.sh          啟動腳本（自動找 Python 環境）
```
