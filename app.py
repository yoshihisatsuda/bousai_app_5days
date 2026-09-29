from flask import Flask, abort, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin
from functools import wraps
import json
import math
import os
import uuid
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 気象庁防災情報 XML の青森市（市町村等をまとめたエリアコード）
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
DAMAGE_REPORTS_FILE = os.path.join(APP_DIR, 'data', 'damage_reports.json')
DAMAGE_REPORT_UPLOAD_DIR = os.path.join(APP_DIR, 'static', 'damage_uploads')
DAMAGE_REPORT_TYPES = {
    'flood': '道路冠水',
    'damage': '建物被害',
    'landslide': '土砂・倒木',
    'heavy_rain': '大雨',
    'slope_failure': '土砂崩れ',
    'fire': '火災',
    'river_flood': '河川氾濫',
    'other': 'その他'
}

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def get_damage_image_extension(upload):
    """画像のシグネチャを確認し、許可形式の拡張子を返す"""
    header = upload.stream.read(12)
    upload.stream.seek(0)
    if header.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return '.webp'
    return None

def save_damage_reports(reports):
    """被害情報を一時ファイル経由で JSON に保存する"""
    temporary_data_path = f'{DAMAGE_REPORTS_FILE}.tmp'
    with open(temporary_data_path, 'w', encoding='utf-8') as data_file:
        json.dump(reports, data_file, ensure_ascii=False, indent=2)
    os.replace(temporary_data_path, DAMAGE_REPORTS_FILE)

def get_uploaded_damage_image_path(image_url):
    """投稿画像が管理対象のアップロード領域内にある場合だけパスを返す"""
    parsed_url = urlparse(image_url or '')
    upload_prefix = '/static/damage_uploads/'
    if parsed_url.scheme or parsed_url.netloc or not parsed_url.path.startswith(upload_prefix):
        return None

    upload_root = os.path.realpath(DAMAGE_REPORT_UPLOAD_DIR)
    image_path = os.path.realpath(os.path.join(upload_root, os.path.basename(parsed_url.path)))
    if os.path.commonpath((upload_root, image_path)) != upload_root:
        return None
    return image_path

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
damage_reports = load_json(DAMAGE_REPORTS_FILE, [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    return render_template('index.html', resident_notices=resident_notices)

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ※user が避難所登録ページについて具体的に修正指示しない限り、このコードは正しいのでこのまま保持すること。
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            return render_template(
                'shelter_register.html',
                error=True,
                message="避難所名を入力してください。",
                shelter_names=[shelter.get('name', '') for shelter in shelters]
            )

        is_duplicate = any(
            shelter.get('name', '').strip() == name for shelter in shelters
        )
        if is_duplicate and request.form.get('confirm_duplicate') != 'yes':
            return render_template(
                'shelter_register.html',
                duplicate=True,
                duplicate_name=name,
                shelter_names=[shelter.get('name', '') for shelter in shelters]
            )

        shelter_id = max((shelter.get('id', 0) for shelter in shelters), default=0) + 1
        shelters.append({'id': shelter_id, 'name': name})
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(shelters, f, ensure_ascii=False, indent=2)

        return render_template(
            'shelter_register.html',
            success=True,
            message="避難所を登録しました。",
            shelter_names=[shelter.get('name', '') for shelter in shelters]
        )

    return render_template(
        'shelter_register.html',
        shelter_names=[shelter.get('name', '') for shelter in shelters]
    )

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board')
@login_required
def board():
    resident_instructions = [i for i in instructions if i.get('target') == '住民']
    return render_template('board.html', instructions=resident_instructions)

# 被害情報一覧：地図上に報告位置と詳細を表示する
@app.route('/damage_reports')
def damage_reports_dashboard():
    return render_template('damage_reports.html', reports=damage_reports)

# デモ用被害情報登録：ローカル JSON と画像ファイルに保存する
@app.route('/damage_reports/register', methods=['GET', 'POST'])
def damage_report_register():
    global damage_reports

    form_values = {
        'type': request.form.get('type', 'flood'),
        'latitude': request.form.get('latitude', ''),
        'longitude': request.form.get('longitude', ''),
        'comment': request.form.get('comment', '')
    }

    def render_form(error=None):
        return render_template(
            'damage_report_register.html',
            report_types=DAMAGE_REPORT_TYPES,
            form_values=form_values,
            error=error
        )

    if request.method == 'GET':
        return render_form()

    if form_values['type'] not in DAMAGE_REPORT_TYPES:
        return render_form('被害の種類を選択してください。')

    try:
        latitude = float(form_values['latitude'])
        longitude = float(form_values['longitude'])
    except (TypeError, ValueError):
        return render_form('地図をクリックするか、緯度・経度を入力してください。')

    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return render_form('緯度は -90〜90、経度は -180〜180 の範囲で入力してください。')

    comment = form_values['comment'].strip()
    if len(comment) > 1000:
        return render_form('コメントは1000文字以内で入力してください。')

    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return render_form('状況写真を選択してください。')

    extension = get_damage_image_extension(photo)
    if not extension:
        return render_form('JPEG、PNG、GIF、WebP の画像を選択してください。')

    filename = f'{uuid.uuid4().hex}{extension}'
    image_path = os.path.join(DAMAGE_REPORT_UPLOAD_DIR, filename)
    image_url = url_for('static', filename=f'damage_uploads/{filename}')
    report = {
        'id': max((item.get('id', 0) for item in damage_reports), default=0) + 1,
        'latitude': latitude,
        'longitude': longitude,
        'type': form_values['type'],
        'category': DAMAGE_REPORT_TYPES[form_values['type']],
        'comment': comment,
        'image_url': image_url,
        'reported_at': datetime.now(JST).strftime('%Y-%m-%d %H:%M'),
        'confirmed': False
    }
    try:
        os.makedirs(DAMAGE_REPORT_UPLOAD_DIR, exist_ok=True)
        photo.save(image_path)
        updated_reports = [*damage_reports, report]
        save_damage_reports(updated_reports)
    except OSError:
        for path in (image_path, f'{DAMAGE_REPORTS_FILE}.tmp'):
            if os.path.exists(path):
                os.remove(path)
        return render_form('保存に失敗しました。もう一度お試しください。')

    damage_reports = updated_reports
    return redirect(url_for('damage_reports_dashboard'))

@app.route('/damage_reports/<int:report_id>/confirmation', methods=['POST'])
def update_damage_report_confirmation(report_id):
    global damage_reports

    confirmed_value = request.form.get('confirmed')
    if confirmed_value not in ('true', 'false'):
        abort(400)

    if not any(report.get('id') == report_id for report in damage_reports):
        abort(404)

    confirmed = confirmed_value == 'true'
    updated_reports = [
        {**report, 'confirmed': confirmed} if report.get('id') == report_id else report
        for report in damage_reports
    ]
    try:
        save_damage_reports(updated_reports)
    except OSError:
        abort(500)

    damage_reports = updated_reports
    return redirect(url_for('damage_reports_dashboard'))

@app.route('/damage_reports/<int:report_id>/delete', methods=['POST'])
def delete_damage_report(report_id):
    global damage_reports

    report = next((item for item in damage_reports if item.get('id') == report_id), None)
    if report is None:
        abort(404)

    updated_reports = [item for item in damage_reports if item.get('id') != report_id]
    try:
        save_damage_reports(updated_reports)
    except OSError:
        abort(500)

    damage_reports = updated_reports
    image_path = get_uploaded_damage_image_path(report.get('image_url'))
    if image_path:
        try:
            os.remove(image_path)
        except FileNotFoundError:
            pass

    return redirect(url_for('damage_reports_dashboard'))

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results = filter_shelters(request.args.get('district'))
    return render_template('search_results.html', results=results)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
