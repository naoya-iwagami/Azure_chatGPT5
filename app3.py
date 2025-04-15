import os  
import json  
import base64  
import threading  
import datetime  
import uuid  
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify  
from flask_session import Session  # Flask-Session をインポート  
from azure.search.documents import SearchClient  
from azure.core.credentials import AzureKeyCredential  
from azure.core.pipeline.transport import RequestsTransport  
from azure.cosmos import CosmosClient  
from openai import AzureOpenAI  
from PIL import Image  
import certifi  
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions  
from werkzeug.utils import secure_filename  
import markdown2  # 'markdown2' をインポート  
  
# Flask の初期化  
app = Flask(__name__)  
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-default-secret-key')  # 環境変数からシークレットキーを取得  
  
# セッションをサーバーサイドで保存する設定（ファイルシステムを利用）  
app.config['SESSION_TYPE'] = 'filesystem'  
app.config['SESSION_FILE_DIR'] = os.path.join(os.getcwd(), 'flask_session')  
app.config['SESSION_PERMANENT'] = False  
Session(app)  
  
# Azure OpenAI の設定（環境変数から取得）  
client = AzureOpenAI(  
    api_key=os.getenv("AZURE_OPENAI_KEY"),  
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),  
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")  
)  
  
# Azure Cognitive Search の設定  
search_service_endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")  
search_service_key = os.getenv("AZURE_SEARCH_KEY")  
transport = RequestsTransport(verify=certifi.where())  
  
# Cosmos DB の設定  
cosmos_endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")  
cosmos_key = os.getenv("AZURE_COSMOS_KEY")  
database_name = 'chatdb'  
container_name = 'personalchats'  
cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)  
database = cosmos_client.get_database_client(database_name)  
container = database.get_container_client(container_name)  
  
# Azure Blob Storage の設定  
blob_connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")  
blob_service_client = BlobServiceClient.from_connection_string(blob_connection_string)  
  
# 使用するコンテナ名（予め作成済み）  
image_container_name = 'chatgpt-image'  
image_container_client = blob_service_client.get_container_client(image_container_name)  
  
# 排他制御用ロック  
lock = threading.Lock()  
  
# デバッグ用関数：受信したクレーム情報を出力  
def debug_authenticated_user():  
    principal_header = request.headers.get("X-MS-CLIENT-PRINCIPAL")  
    if not principal_header:  
        debug_info = "ヘッダー 'X-MS-CLIENT-PRINCIPAL' が存在しません。"  
        print(debug_info)  
        return debug_info  
    try:  
        decoded = base64.b64decode(principal_header).decode("utf-8")  
        user_data = json.loads(decoded)  
        debug_info = json.dumps(user_data, indent=4, ensure_ascii=False)  
    except Exception as e:  
        debug_info = "X-MS-CLIENT-PRINCIPAL のデコードに失敗しました: " + str(e)  
    print(debug_info)  
    return debug_info  
  
# SAS トークン付き URL 生成関数  
def generate_sas_url(blob_client, blob_name):  
    sas_token = generate_blob_sas(  
        account_name=blob_client.account_name,  
        container_name=blob_client.container_name,  
        blob_name=blob_name,  
        account_key=blob_client.credential.account_key,  
        permission=BlobSasPermissions(read=True),  
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=1)  
    )  
    return f"{blob_client.url}?{sas_token}"  
  
# Easy Auth (Entra ID) によるユーザー認証処理  
def get_authenticated_user():  
    # セッションに既にユーザー情報があればそれを利用  
    if "user_id" in session and "user_name" in session:  
        return session["user_id"]  
  
    client_principal = request.headers.get("X-MS-CLIENT-PRINCIPAL")  
    if client_principal:  
        try:  
            decoded = base64.b64decode(client_principal).decode("utf-8")  
            user_data = json.loads(decoded)  
            user_id = None  
            user_name = None  
            if "claims" in user_data:  
                for claim in user_data["claims"]:  
                    # Object ID の取得  
                    if claim.get("typ") == "http://schemas.microsoft.com/identity/claims/objectidentifier":  
                        user_id = claim.get("val")  
                    # ユーザー プリンシパル名の取得  
                    if claim.get("typ") == "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn":  
                        user_name = claim.get("val")  
            if user_id:  
                session["user_id"] = user_id  
            if user_name:  
                session["user_name"] = user_name  
            return user_id  
        except Exception as e:  
            print("Easy Auth ユーザー情報の取得エラー:", e)  
  
    # 取得できなかった場合は anonymous として扱う  
    session["user_id"] = "anonymous@example.com"  
    session["user_name"] = "anonymous"  
    return session["user_id"]  
  
# Cosmos DB へチャット履歴を保存する関数  
def save_chat_history():  
    with lock:  
        try:  
            sidebar = session.get("sidebar_messages", [])  
            idx = session.get("current_chat_index", 0)  
            if idx < len(sidebar):  
                current = sidebar[idx]  
                user_id = get_authenticated_user()  
                user_name = session.get("user_name", "anonymous")  
                session_id = current.get("session_id")  
                item = {  
                    'id': session_id,  
                    'user_id': user_id,  
                    'user_name': user_name,  # ユーザー プリンシパル名を追加  
                    'session_id': session_id,  
                    'messages': current.get("messages", []),  
                    'system_message': current.get("system_message", session.get("default_system_message", "あなたは親切なAIアシスタントです。ユーザーの質問に簡潔かつ正確に答えてください。")),  
                    'first_assistant_message': current.get("first_assistant_message", ""),  
                    'timestamp': datetime.datetime.utcnow().isoformat()  
                }  
                container.upsert_item(item)  
        except Exception as e:  
            print(f"チャット履歴保存エラー: {e}")  
  
# Cosmos DB からチャット履歴を読み込む関数  
def load_chat_history():  
    with lock:  
        user_id = get_authenticated_user()  
        sidebar_messages = []  
        try:  
            query = "SELECT * FROM c WHERE c.user_id = @user_id ORDER BY c.timestamp DESC"  
            parameters = [{"name": "@user_id", "value": user_id}]  
            items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)  
            for item in items:  
                if 'session_id' in item:  
                    chat = {  
                        "session_id": item['session_id'],  
                        "messages": item.get("messages", []),  
                        "system_message": item.get("system_message", session.get('default_system_message', "あなたは親切なAIアシスタントです。ユーザーの質問に簡潔かつ正確に答えてください。")),  
                        "first_assistant_message": item.get("first_assistant_message", ""),  
                    }  
                    sidebar_messages.append(chat)  
        except Exception as e:  
            print(f"チャット履歴読み込みエラー: {e}")  
        return sidebar_messages  
  
# 新しいチャットセッションを開始する関数  
def start_new_chat():  
    # 既存画像の削除  
    image_filenames = session.get("image_filenames", [])  
    for img_name in image_filenames:  
        blob_client = image_container_client.get_blob_client(img_name)  
        try:  
            blob_client.delete_blob()  
        except Exception as e:  
            print("画像削除エラー:", e)  
    session["image_filenames"] = []  
  
    new_session_id = str(uuid.uuid4())  
    new_chat = {  
        "session_id": new_session_id,  
        "messages": [],  
        "first_assistant_message": "",  
        "system_message": session.get('default_system_message', "あなたは親切なAIアシスタントです。ユーザーの質問に簡潔かつ正確に答えてください。")  
    }  
    sidebar = session.get("sidebar_messages", [])  
    sidebar.insert(0, new_chat)  
    session["sidebar_messages"] = sidebar  
    session["current_chat_index"] = 0  
    session["main_chat_messages"] = []  # メインコンテンツエリア用のチャットリスト  
    session.modified = True  
  
# Blob 内の画像を Base64 エンコードする関数  
def encode_image_from_blob(blob_client):  
    downloader = blob_client.download_blob()  
    image_data = downloader.readall()  
    return base64.b64encode(image_data).decode('utf-8')  
  
@app.route('/', methods=['GET', 'POST'])  
def index():  
    # ユーザー認証（Easy Auth から user_id を取得）  
    get_authenticated_user()  
  
    # 必要に応じてデバッグ情報を取得し、コンソールへ出力する。  
    debug_info = debug_authenticated_user()  
  
    if "default_system_message" not in session:  
        session["default_system_message"] = "あなたは親切なAIアシスタントです。ユーザーの質問に簡潔かつ正確に答えてください。"  
        session.modified = True  
  
    if "sidebar_messages" not in session:  
        session["sidebar_messages"] = load_chat_history() or []  
        session.modified = True  
  
    if "current_chat_index" not in session:  
        start_new_chat()  
        session["show_all_history"] = False  # 履歴の表示をリセット  
        session.modified = True  
  
    if "main_chat_messages" not in session:  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        if sidebar and idx < len(sidebar):  
            session["main_chat_messages"] = sidebar[idx].get("messages", [])  
        else:  
            session["main_chat_messages"] = []  
        session.modified = True  
  
    if "image_filenames" not in session:  
        session["image_filenames"] = []  
        session.modified = True  
  
    if "show_all_history" not in session:  
        session["show_all_history"] = False  
        session.modified = True  
  
    # POST リクエストの処理（主にチャット履歴、画像アップロード、チャットセッションの管理）  
    if request.method == 'POST':  
        # 新しいチャットの開始  
        if 'new_chat' in request.form:  
            start_new_chat()  
            session["show_all_history"] = False  
            session.modified = True  
            return redirect(url_for('index'))  
  
        # サイドバーからのチャット選択（session_id が送信される）  
        if 'select_chat' in request.form:  
            selected_session = request.form.get("select_chat")  
            sidebar = session.get("sidebar_messages", [])  
            for idx, chat in enumerate(sidebar):  
                if chat.get("session_id") == selected_session:  
                    session["current_chat_index"] = idx  
                    session["main_chat_messages"] = chat.get("messages", [])  
                    break  
            session.modified = True  
            return redirect(url_for('index'))  
  
        # 履歴の表示切替  
        if 'toggle_history' in request.form:  
            session["show_all_history"] = not session.get("show_all_history", False)  
            session.modified = True  
            return redirect(url_for('index'))  
  
        # 画像アップロード処理  
        if 'upload_images' in request.form:  
            if 'images' in request.files:  
                files = request.files.getlist("images")  
                image_filenames = session.get("image_filenames", [])  
                for file in files:  
                    if file and file.filename != '':  
                        try:  
                            filename = secure_filename(file.filename)  
                            blob_client = image_container_client.get_blob_client(filename)  
                            file.stream.seek(0)  
                            blob_client.upload_blob(file.stream, overwrite=True)  
                            if filename not in image_filenames:  
                                image_filenames.append(filename)  
                        except Exception as e:  
                            print("画像アップロードエラー:", e)  
                session["image_filenames"] = image_filenames  
                session.modified = True  
            return redirect(url_for('index'))  
  
        # アップロード画像削除処理  
        if 'delete_image' in request.form:  
            delete_image_name = request.form.get("delete_image")  
            image_filenames = session.get("image_filenames", [])  
            image_filenames = [name for name in image_filenames if name != delete_image_name]  
            blob_client = image_container_client.get_blob_client(delete_image_name)  
            try:  
                blob_client.delete_blob()  
            except Exception as e:  
                print("画像削除エラー:", e)  
            session["image_filenames"] = image_filenames  
            session.modified = True  
            return redirect(url_for('index'))  
  
        # ※ユーザーのチャット送信処理は AJAX を用いるため、ここでは処理しない  
  
    # GET リクエストの処理  
    chat_history = session.get("main_chat_messages", [])  
    sidebar_messages = session.get("sidebar_messages", [])  
    image_filenames = session.get("image_filenames", [])  
    images = []  
    for filename in image_filenames:  
        blob_client = image_container_client.get_blob_client(filename)  
        image_url = generate_sas_url(blob_client, filename)  
        images.append({'name': filename, 'url': image_url})  
  
    max_displayed_history = 5  
    max_total_history = 20  
    show_all_history = session.get("show_all_history", False)  
  
    # render_template に debug_info を渡す（index.html で先頭に表示できるようにしてください）  
    return render_template(  
        'index.html',  
        chat_history=chat_history,  
        chat_sessions=sidebar_messages,  
        images=images,  
        show_all_history=show_all_history,  
        max_displayed_history=max_displayed_history,  
        max_total_history=max_total_history,  
        debug_info=debug_info  
    )  
  
@app.route('/send_message', methods=['POST'])  
def send_message():  
    data = request.get_json()  
    prompt = data.get('prompt', '').strip()  
    if not prompt:  
        return json.dumps({'response': ''}), 400, {'Content-Type': 'application/json'}  
  
    # ユーザーのメッセージを session に追加する  
    messages = session.get("main_chat_messages", [])  
    messages.append({"role": "user", "content": prompt})  
    session["main_chat_messages"] = messages  
    session.modified = True  
  
    # チャット履歴の保存（Cosmos DB への更新）  
    save_chat_history()  
  
    try:  
        # Azure Cognitive Search を利用した関連ドキュメント検索  
        index_name = "filetest11"  
        search_client = SearchClient(  
            endpoint=search_service_endpoint,  
            index_name=index_name,  
            credential=AzureKeyCredential(search_service_key),  
            transport=transport  
        )  
        topNDocuments = 5  
        strictness = 0.1  
        search_results = search_client.search(  
            search_text=prompt,  
            search_fields=["content", "title"],  
            select="content,filepath,title,url",  
            query_type="semantic",  
            semantic_configuration_name="default",  
            query_caption="extractive",  
            query_answer="extractive",  
            top=topNDocuments  
        )  
        results_list = [result for result in search_results if result['@search.score'] >= strictness]  
        results_list.sort(key=lambda x: x['@search.score'], reverse=True)  
        context = "\n".join(  
            [f"ファイル名: {result.get('title', '不明')}\n内容: {result['content']}" for result in results_list]  
        )  
  
        rule_message = (  
            "回答する際は、以下のルールに従ってください：\n"  
            "1. 簡潔かつ正確に回答してください。\n"  
            "2. 必要に応じて、提供されたコンテキストを参照してください。\n"  
        )  
  
        # メッセージリスト作成  
        messages_list = []  
        # 現在のチャットのシステム・メッセージを利用  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        system_msg = sidebar[idx].get("system_message", session.get("default_system_message"))  
        messages_list.append({"role": "system", "content": system_msg})  
        messages_list.append({"role": "user", "content": rule_message})  
        messages_list.append({"role": "user", "content": f"以下のコンテキストを参考にしてください: {context[:15000]}"})  
        past_message_count = 10  
        messages_list.extend(session.get("main_chat_messages", [])[-(past_message_count * 2):])  
  
        # 画像アップロード情報があれば追加  
        image_filenames = session.get("image_filenames", [])  
        if image_filenames:  
            image_contents = []  
            for img_name in image_filenames:  
                blob_client = image_container_client.get_blob_client(img_name)  
                try:  
                    encoded_image = encode_image_from_blob(blob_client)  
                    ext = img_name.rsplit('.', 1)[1].lower()  
                    mime_type = f"image/{ext}" if ext in ['png', 'jpeg', 'jpg', 'gif'] else 'application/octet-stream'  
                    data_url = f"data:{mime_type};base64,{encoded_image}"  
                    image_contents.append({  
                        "type": "image_url",  
                        "image_url": {"url": data_url}  
                    })  
                except Exception as e:  
                    print("画像エンコードエラー:", e)  
            messages_list[2]["content"] = [{"type": "text", "text": messages_list[2]["content"]}] + image_contents  
  
        response_obj = client.chat.completions.create(  
            model="gpt-4o",  
            messages=messages_list,  
        )  
        assistant_response = response_obj.choices[0].message.content  
  
        # 'markdown2' を使用して Markdown を HTML に変換  
        assistant_response_html = markdown2.markdown(  
            assistant_response,  
            extras=["tables", "fenced-code-blocks", "code-friendly", "break-on-newline", "cuddled-lists"]  
        )  
  
        # アシスタント返答を main_chat_messages に追加  
        messages.append({"role": "assistant", "content": assistant_response_html, "type": "html"})  
        session["main_chat_messages"] = messages  
        session.modified = True  
  
        # sidebar_messages 内の該当セッションも更新  
        idx = session.get("current_chat_index", 0)  
        if idx < len(sidebar):  
            sidebar[idx]["messages"] = messages  
            if not sidebar[idx].get("first_assistant_message"):  
                sidebar[idx]["first_assistant_message"] = assistant_response  
            session["sidebar_messages"] = sidebar  
            session.modified = True  
  
        # チャット履歴の保存  
        save_chat_history()  
        session["assistant_responded"] = True  
        session.modified = True  
  
        return json.dumps({'response': assistant_response_html}), 200, {'Content-Type': 'application/json'}  
    except Exception as e:  
        print("チャット応答エラー:", e)  
        flash(f"エラーが発生しました: {e}", "error")  
        session["assistant_responded"] = True  
        session.modified = True  
        return json.dumps({'response': f"エラーが発生しました: {e}"}), 500, {'Content-Type': 'application/json'}  
  
if __name__ == '__main__':  
    app.run(debug=True, host='0.0.0.0')  