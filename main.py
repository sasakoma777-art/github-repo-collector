import os
import time
import json
import base64
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google import genai
from google.genai import types

# --------------------------------------------------
# 環境設定
# --------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
SPREADSHEET_KEY = os.environ.get("SPREADSHEET_KEY")
GCP_CREDS_JSON = os.environ.get("GCP_CREDS_JSON")

KEYWORDS = [
    "accounting stars:>100 license:mit",
    "crm stars:>200 license:mit",
    "time-tracking stars:>50 license:mit",
    "booking stars:>100 license:mit",
    "form-builder stars:>100 license:mit",
    "e-signature stars:>50 license:mit",
    "helpdesk stars:>100 license:mit",
    "kanban stars:>200 license:mit",
    "workflow automation stars:>300 license:mit",
    "landing-page template stars:>100 license:mit",
    "admin dashboard stars:>500 license:mit",
    "seo tool stars:>100 license:mit",
    "email marketing stars:>100 license:mit",
    "social media automation stars:>100 license:mit",
    "markdown editor stars:>200 license:mit"
]

def init_google_sheet():
    """Googleスプレッドシートの認証と取得"""
    creds_dict = json.loads(GCP_CREDS_JSON)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_KEY).sheet1
    return sheet

def safe_append_row(sheet, row, max_retries=3):
    """スプレッドシート書き込み時の503/一時エラー自動リトライ"""
    for attempt in range(1, max_retries + 1):
        try:
            sheet.append_row(row)
            return True
        except Exception as e:
            print(f"  [Sheets API エラー] 再試行します ({attempt}/{max_retries}): {e}")
            time.sleep(5 * attempt)
    return False

def search_github(query, per_page=15):
    """GitHub Search APIでリポジトリを検索"""
    url = "https://api.github.com/search/repositories"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Repo-Collector"
    }
    if GITHUB_TOKEN and GITHUB_TOKEN.strip():
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN.strip()}"
    
    params = {
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": per_page
    }
    
    res = requests.get(url, headers=headers, params=params)
    if res.status_code == 200:
        items = res.json().get("items", [])
        print(f"  -> {len(items)} 件のリポジトリを取得しました")
        return items
    else:
        print(f"  [GitHub API Error] Status: {res.status_code}, Response: {res.text}")
        return []

def get_readme(repo_full_name):
    """READMEテキストを取得（先頭1500文字）"""
    url = f"https://api.github.com/repos/{repo_full_name}/readme"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Repo-Collector"
    }
    if GITHUB_TOKEN and GITHUB_TOKEN.strip():
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN.strip()}"
    
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        content = res.json().get("content", "")
        decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
        return decoded[:1500]
    return ""

def analyze_with_gemini(client, repo, readme, max_retries=3):
    """Gemini API（gemini-3.6-flash）で構造化データを抽出（429/503自動再試行）"""
    prompt = f"""
以下のGitHubリポジトリ情報を読み取り、指定形式で分類・要約してください。

【リポジトリ情報】
- 名前: {repo['full_name']}
- 概要: {repo.get('description') or 'なし'}
- README抜粋:
{readme}

【出力要件】
1. level: 「入門・副業」「中級」「上級」から選択
2. genre: 2〜8文字の日本語カテゴリ（例: お金・請求, 顧客管理, 業務自動化）
3. difficulty: 「◎」「◯」「△」のいずれか
4. summary: 30〜50文字程度の日本語要約
"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "level": {"type": "STRING"},
            "genre": {"type": "STRING"},
            "difficulty": {"type": "STRING"},
            "summary": {"type": "STRING"}
        },
        "required": ["level", "genre", "difficulty", "summary"]
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model='gemini-3.6-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            err_msg = str(e)
            # 429（制限）または 503（高負荷・一時不通）の場合は待機して再試行
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "503" in err_msg or "UNAVAILABLE" in err_msg:
                wait_sec = 20 * attempt  # 20秒、40秒、60秒と徐々に待機時間を拡大
                print(f"  [API一時制限/混雑] {wait_sec}秒待機して再試行します... ({attempt}/{max_retries})")
                time.sleep(wait_sec)
            else:
                print(f"  [Gemini API Error]: {e}")
                return None
    return None

def main():
    print("=== GitHub定期収集スクリプト起動 ===")
    sheet = init_google_sheet()
    
    # 既存リポジトリURLの取得（重複登録防止用）
    existing_urls = set(sheet.col_values(1))
    print(f"既存の登録件数: {len(existing_urls)} 件")
    
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
    
    for kw in KEYWORDS:
        print(f"--- 検索中: {kw} ---")
        repos = search_github(kw, per_page=15)
        
        for repo in repos:
            repo_url = repo["html_url"]
            if repo_url in existing_urls:
                print(f"スキップ（登録済）: {repo['full_name']}")
                continue
            
            print(f"解析中: {repo['full_name']}")
            readme = get_readme(repo["full_name"])
            ai_data = analyze_with_gemini(ai_client, repo, readme)
            
            if ai_data:
                license_name = repo["license"]["spdx_id"] if repo.get("license") else "なし"
                row = [
                    repo_url,
                    ai_data.get("level", "入門・副業"),
                    ai_data.get("genre", "未分類"),
                    ai_data.get("difficulty", "◯"),
                    repo["full_name"],
                    repo["stargazers_count"],
                    ai_data.get("summary", repo.get("description") or ""),
                    license_name
                ]
                
                # スプレッドシートへ安全に追加
                if safe_append_row(sheet, row):
                    existing_urls.add(repo_url)
                    print(f"追加完了: {repo['full_name']}")
            
            # API無料枠の安全マージンとして15秒待機
            time.sleep(15)

if __name__ == "__main__":
    main()
