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
    "accounting stars:>200 license:mit",
    "crm stars:>300 license:mit",
    "time-tracking stars:>100 license:mit",
    "booking stars:>200 license:mit",
    "form-builder stars:>200 license:mit",
    "e-signature stars:>100 license:mit",
    "helpdesk stars:>200 license:mit",
    "kanban stars:>300 license:mit",
    "workflow automation stars:>500 license:mit",
    "portfolio-template stars:>100 license:mit"
]

def init_google_sheet():
    """Googleスプレッドシートの認証と取得"""
    creds_dict = json.loads(GCP_CREDS_JSON)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_KEY).sheet1
    return sheet

def search_github(query, per_page=5):
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
    """Gemini API（gemini-3.6-flash）で構造化データを抽出"""
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
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print(f"  [Rate limit 429] 15秒待機して再試行します... ({attempt}/{max_retries})")
                time.sleep(15)
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
        repos = search_github(kw, per_page=5)
        
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
                sheet.append_row(row)
                existing_urls.add(repo_url)
                print(f"追加完了: {repo['full_name']}")
            
            # API無料枠の制限を超えないよう13秒待機
            time.sleep(13)

if __name__ == "__main__":
    main()
