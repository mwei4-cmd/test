# PCB Nesting Tool

## 本地執行

```bash
pip install -r requirements.txt
python main.py
# 開啟 http://localhost:8000
```

## 部署到 Render（免費公開網址）

### 步驟一：上傳到 GitHub

1. 去 [github.com](https://github.com) 建立新 repo（例如 `pcb-nesting-tool`）
2. 把這個資料夾的所有檔案上傳進去：
   - `main.py`
   - `requirements.txt`
   - `render.yaml`
   - `static/index.html`

   上傳方式：GitHub 網頁 → 拖拽檔案，或用 git：
   ```bash
   git init
   git add .
   git commit -m "init"
   git remote add origin https://github.com/你的帳號/pcb-nesting-tool.git
   git push -u origin main
   ```

### 步驟二：在 Render 部署

1. 去 [render.com](https://render.com) 註冊（用 GitHub 帳號登入最快）
2. 點 **New +** → **Web Service**
3. 選 **Connect a repository** → 選你剛剛建立的 repo
4. 設定如下：
   - **Name**：pcb-nesting-tool（或任意名稱）
   - **Runtime**：Python 3
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`uvicorn main:app --host 0.0.0.0 --port $PORT`
5. 點 **Create Web Service**

Render 會自動部署，約 2-3 分鐘後給你一個網址：
`https://pcb-nesting-tool.onrender.com`

### 注意事項

- Render 免費方案閒置 15 分鐘後會休眠，第一次訪問需等約 30 秒喚醒
- 免費方案每月有 750 小時的執行時間（單個服務足夠）
- 如果需要一直保持喚醒，可以用 [UptimeRobot](https://uptimerobot.com) 每 10 分鐘 ping 一次你的網址

## 檔案結構

```
pcb_app/
├── main.py          # FastAPI backend（所有排版、橋接邏輯）
├── requirements.txt # Python 套件
├── render.yaml      # Render 部署設定
└── static/
    └── index.html   # 前端介面（單頁應用）
```
