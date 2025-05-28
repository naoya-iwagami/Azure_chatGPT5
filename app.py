import os  
import json  
import base64  
import threading  
import datetime  
import uuid  
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify  
from flask_session import Session  
from azure.search.documents import SearchClient  
from azure.core.credentials import AzureKeyCredential  
from azure.core.pipeline.transport import RequestsTransport  
from azure.cosmos import CosmosClient  
from openai import AzureOpenAI  
from PIL import Image  
import certifi  
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions  
from werkzeug.utils import secure_filename  
import markdown2
  
app = Flask(__name__)  
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-default-secret-key')  
app.config['SESSION_TYPE'] = 'filesystem'  
app.config['SESSION_FILE_DIR'] = os.path.join(os.getcwd(), 'flask_session')  
app.config['SESSION_PERMANENT'] = False  
Session(app)  
  
client = AzureOpenAI(  
    api_key=os.getenv("AZURE_OPENAI_KEY"),  
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),  
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")  
)  
  
search_service_endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")  
search_service_key = os.getenv("AZURE_SEARCH_KEY")  
transport = RequestsTransport(verify=certifi.where())  
  
cosmos_endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")  
cosmos_key = os.getenv("AZURE_COSMOS_KEY")  
database_name = 'chatdb'  
container_name = 'personalchats'  
cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)  
database = cosmos_client.get_database_client(database_name)  
container = database.get_container_client(container_name)  
  
blob_connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")  
blob_service_client = BlobServiceClient.from_connection_string(blob_connection_string)  
image_container_name = 'chatgpt-image'  
image_container_client = blob_service_client.get_container_client(image_container_name)  
  
lock = threading.Lock()  
  
def generate_sas_url(blob_client, blob_name):  
    storage_account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")  
    if not storage_account_key:  
        raise Exception("AZURE_STORAGE_ACCOUNT_KEY が設定されていません。")  
    sas_token = generate_blob_sas(  
        account_name=blob_client.account_name,  
        container_name=blob_client.container_name,  
        blob_name=blob_name,  
        account_key=storage_account_key,  
        permission=BlobSasPermissions(read=True),  
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=1)  
    )  
    return f"{blob_client.url}?{sas_token}"  
  
def get_authenticated_user():  
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
                    if claim.get("typ") == "http://schemas.microsoft.com/identity/claims/objectidentifier":  
                        user_id = claim.get("val")  
                    if claim.get("typ") == "name":  
                        user_name = claim.get("val")  
            if user_id:  
                session["user_id"] = user_id  
            if user_name:  
                session["user_name"] = user_name  
            return user_id  
        except Exception as e:  
            print("Easy Auth ユーザー情報の取得エラー:", e)  
    session["user_id"] = "anonymous@example.com"  
    session["user_name"] = "anonymous"  
    return session["user_id"]  
  
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
                    'user_name': user_name,  
                    'session_id': session_id,  
                    'messages': current.get("messages", []),  
                    'system_message': current.get("system_message", session.get("default_system_message", "あなたは親切なAIアシスタントです。ユーザーの質問が不明確な場合は、「こういうことですか？」と内容を確認してください。質問が明確な場合は、簡潔かつ正確に答えてください。")),  
                    'first_assistant_message': current.get("first_assistant_message", ""),  
                    'timestamp': datetime.datetime.utcnow().isoformat()  
                }  
                container.upsert_item(item)  
        except Exception as e:  
            print(f"チャット履歴保存エラー: {e}")  
  
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
                        "system_message": item.get("system_message", session.get('default_system_message', "あなたは親切なAIアシスタントです。ユーザーの質問が不明確な場合は、「こういうことですか？」と内容を確認してください。質問が明確な場合は、簡潔かつ正確に答えてください。")),  
                        "first_assistant_message": item.get("first_assistant_message", ""),  
                    }  
                    sidebar_messages.append(chat)  
        except Exception as e:  
            print(f"チャット履歴読み込みエラー: {e}")  
        return sidebar_messages  
  
def start_new_chat():  
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
        "system_message": session.get('default_system_message', "あなたは親切なAIアシスタントです。ユーザーの質問が不明確な場合は、「こういうことですか？」と内容を確認してください。質問が明確な場合は、簡潔かつ正確に答えてください。")  
    }  
    sidebar = session.get("sidebar_messages", [])  
    sidebar.insert(0, new_chat)  
    session["sidebar_messages"] = sidebar  
    session["current_chat_index"] = 0  
    session["main_chat_messages"] = []  
    session.modified = True  
  
def encode_image_from_blob(blob_client):  
    downloader = blob_client.download_blob()  
    image_data = downloader.readall()  
    return base64.b64encode(image_data).decode('utf-8')  
  
@app.route('/', methods=['GET', 'POST'])  
def index():  
    get_authenticated_user()  
    if "default_system_message" not in session:  
        session["default_system_message"] = "あなたは親切なAIアシスタントです。ユーザーの質問が不明確な場合は、「こういうことですか？」と内容を確認してください。質問が明確な場合は、簡潔かつ正確に答えてください。"  
        session.modified = True  
    if "sidebar_messages" not in session:  
        session["sidebar_messages"] = load_chat_history() or []  
        session.modified = True  
    if "current_chat_index" not in session:  
        start_new_chat()  
        session["show_all_history"] = False  
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
    if "conversation_mode" not in session:  
        session["conversation_mode"] = "qa"  
        session.modified = True  
  
    if request.method == 'POST':  
        # 会話モードの変更  
        if 'set_conversation_mode' in request.form:  
            mode = request.form.get("conversation_mode", "qa")  
            session["conversation_mode"] = mode  
            session.modified = True  
            return redirect(url_for('index'))  
  
        # システムメッセージの変更  
        if 'set_system_message' in request.form:  
            sys_msg = request.form.get("system_message", "").strip()  
            # 修正: 空欄もそのまま保存する  
            session["default_system_message"] = sys_msg  
            # 現在のチャットにも反映  
            idx = session.get("current_chat_index", 0)  
            sidebar = session.get("sidebar_messages", [])  
            if sidebar and idx < len(sidebar):  
                sidebar[idx]["system_message"] = session["default_system_message"]  
                session["sidebar_messages"] = sidebar  
            session.modified = True  
            return redirect(url_for('index'))  
  
        if 'new_chat' in request.form:  
            start_new_chat()  
            session["show_all_history"] = False  
            session.modified = True  
            return redirect(url_for('index'))  
  
        if 'select_chat' in request.form:  
            selected_session = request.form.get("select_chat")  
            sidebar = session.get("sidebar_messages", [])  
            for idx, chat in enumerate(sidebar):  
                if chat.get("session_id") == selected_session:  
                    session["current_chat_index"] = idx  
                    session["main_chat_messages"] = chat.get("messages", [])  
                    # システムメッセージも切り替え  
                    session["default_system_message"] = chat.get("system_message", session.get("default_system_message"))  
                    break  
            session.modified = True  
            return redirect(url_for('index'))  
  
        if 'toggle_history' in request.form:  
            session["show_all_history"] = not session.get("show_all_history", False)  
            session.modified = True  
            return redirect(url_for('index'))  
  
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
  
    chat_history = session.get("main_chat_messages", [])  
    sidebar_messages = session.get("sidebar_messages", [])  
    image_filenames = session.get("image_filenames", [])  
    images = []  
    for filename in image_filenames:  
        blob_client = image_container_client.get_blob_client(filename)  
        image_url = generate_sas_url(blob_client, filename)  
        images.append({'name': filename, 'url': image_url})  
  
    max_displayed_history = 6  
    max_total_history = 50  
    show_all_history = session.get("show_all_history", False)  
  
    return render_template(  
        'index.html',  
        chat_history=chat_history,  
        chat_sessions=sidebar_messages,  
        images=images,  
        show_all_history=show_all_history,  
        max_displayed_history=max_displayed_history,  
        max_total_history=max_total_history,  
        session=session  
    )  
  
@app.route('/send_message', methods=['POST'])  
def send_message():  
    data = request.get_json()  
    prompt = data.get('prompt', '').strip()  
    if not prompt:  
        return json.dumps({'response': ''}), 400, {'Content-Type': 'application/json'}  
  
    messages = session.get("main_chat_messages", [])  
    messages.append({"role": "user", "content": prompt})  
    session["main_chat_messages"] = messages  
    session.modified = True  
  
    save_chat_history()  
  
    try:  
        last2_user = [m["content"] for m in messages if m["role"] == "user"][-2:]  
        last2_ai = [m["content"] for m in messages if m["role"] == "assistant"][-2:]  
        search_chunks = last2_user + last2_ai + [prompt]  
        search_query = "\n".join(search_chunks)  
  
        index_name = "filetest11"  
        search_client = SearchClient(  
            endpoint=search_service_endpoint,  
            index_name=index_name,  
            credential=AzureKeyCredential(search_service_key),  
            transport=transport  
        )  
        topNDocuments = 20  
        strictness = 0.1  
        search_results = search_client.search(  
            search_text=search_query,  
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
        messages_list = []  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        system_msg = sidebar[idx].get("system_message", session.get("default_system_message"))  
        messages_list.append({"role": "system", "content": system_msg})  
        messages_list.append({"role": "user", "content": rule_message})  
        messages_list.append({"role": "user", "content": f"以下のコンテキストを参考にしてください: {context[:50000]}"})  
        past_message_count = 20  
        messages_list.extend(session.get("main_chat_messages", [])[-(past_message_count * 2):])  
  
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
  
        # 会話モードに応じてモデルとパラメータを切り替え  
        mode = session.get("conversation_mode", "qa")  
        if mode == "qa":  
            model_name = "gpt-4.1"  
            extra_args = {}  
        elif mode == "reasoning":  
            model_name = "o4-mini"  
            extra_args = {"reasoning_effort": "medium"}  
        else:  
            model_name = "gpt-4.1"  
            extra_args = {}  
  
        response_obj = client.chat.completions.create(  
            model=model_name,  
            messages=messages_list,  
            **extra_args  
        )  
        assistant_response = response_obj.choices[0].message.content  
  
        assistant_response_html = markdown2.markdown(  
            assistant_response,  
            extras=["tables", "fenced-code-blocks", "code-friendly", "break-on-newline", "cuddled-lists"]  
        )  
  
        messages.append({"role": "assistant", "content": assistant_response_html, "type": "html"})  
        session["main_chat_messages"] = messages  
        session.modified = True  
  
        idx = session.get("current_chat_index", 0)  
        if idx < len(sidebar):  
            sidebar[idx]["messages"] = messages  
            if not sidebar[idx].get("first_assistant_message"):  
                sidebar[idx]["first_assistant_message"] = assistant_response  
            session["sidebar_messages"] = sidebar  
            session.modified = True  
  
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