from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import json, os, shutil, uuid, hashlib

# --- تنظیمات دیتابیس (سازگار با Render و لوکال) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./chat.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

os.makedirs("uploads", exist_ok=True)

# --- توابع هش کردن رمز ---
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hash_password(plain_password) == hashed_password

# --- مدل‌های دیتابیس ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    is_admin = Column(Integer, default=0)
    password = Column(String, nullable=True)
    webcam_image = Column(String, nullable=True)

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer)
    receiver_id = Column(Integer)
    content = Column(Text)
    message_type = Column(String, default="text")
    timestamp = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

# --- مدیریت اتصالات ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}
        self.admins: list[int] = []

    async def connect(self, websocket: WebSocket, user_id: int, is_admin: bool = False):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        if is_admin and user_id not in self.admins:
            self.admins.append(user_id)

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        if user_id in self.admins:
            self.admins.remove(user_id)

    def is_online(self, user_id: int):
        return user_id in self.active_connections

    async def send_personal_message(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
            except:
                pass

    async def broadcast_to_admins(self, message: dict):
        for admin_id in self.admins:
            if admin_id in self.active_connections:
                try:
                    await self.active_connections[admin_id].send_text(json.dumps(message))
                except:
                    pass

manager = ConnectionManager()

# --- روت‌ها ---
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(None)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        
        if user and user.is_admin == 1:
            if not password or not user.password:
                return HTMLResponse("رمز عبور ادمین اشتباه است یا تنظیم نشده.", status_code=400)
            if not verify_password(password, user.password):
                return HTMLResponse("رمز عبور ادمین اشتباه است.", status_code=400)
        
        if not user:
            user = User(username=username, is_admin=0)
            db.add(user)
            db.commit()
            db.refresh(user)

        response = RedirectResponse(url="/admin" if user.is_admin == 1 else "/chat", status_code=303)
        response.set_cookie(key="user_id", value=str(user.id))
        return response
    finally:
        db.close()

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id: 
        return RedirectResponse(url="/", status_code=303)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(user_id)).first()
        
        if user is None:
            response = RedirectResponse(url="/", status_code=303)
            response.delete_cookie("user_id")
            return response
        
        admin = db.query(User).filter(User.is_admin == 1).first()
        admin_id = admin.id if admin else 1
        
        messages = db.query(Message).filter(
            ((Message.sender_id == user.id) & (Message.receiver_id == admin_id)) |
            ((Message.sender_id == admin_id) & (Message.receiver_id == user.id))
        ).order_by(Message.timestamp).all()

        return templates.TemplateResponse(request=request, name="index.html", context={
            "user_id": user.id, "admin_id": admin_id, "messages": messages
        })
    finally:
        db.close()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id: 
        return RedirectResponse(url="/", status_code=303)

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.id == int(user_id)).first()
        
        if not admin or admin.is_admin != 1: 
            response = RedirectResponse(url="/", status_code=303)
            response.delete_cookie("user_id")
            return response

        if not admin.password:
            return RedirectResponse(url="/set_password", status_code=303)

        users = db.query(User).filter(User.is_admin == 0).all()
        users_data = [{"id": u.id, "username": u.username, "is_online": manager.is_online(u.id), "webcam": u.webcam_image} for u in users]
        
        return templates.TemplateResponse(request=request, name="admin.html", context={
            "users": users_data, "admin_id": user_id
        })
    finally:
        db.close()

@app.get("/set_password", response_class=HTMLResponse)
async def set_password_page(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id: return RedirectResponse(url="/")
    return templates.TemplateResponse(request=request, name="set_password.html", context={"user_id": user_id})

@app.post("/set_password")
async def set_password(request: Request, new_password: str = Form(...)):
    user_id = request.cookies.get("user_id")
    if len(new_password) < 8:
        return HTMLResponse("رمز عبور باید حداقل ۸ کاراکتر باشد.", status_code=400)
    
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.id == int(user_id), User.is_admin == 1).first()
        if admin:
            admin.password = hash_password(new_password)
            db.commit()
        return RedirectResponse(url="/admin", status_code=303)
    finally:
        db.close()

@app.post("/api/rename_user")
async def rename_user(user_id: int = Form(...), new_name: str = Form(...)):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            u.username = new_name
            db.commit()
        return {"status": "ok"}
    finally:
        db.close()

@app.post("/api/delete_history")
async def delete_history(user_id: int = Form(...), admin_id: int = Form(...)):
    db = SessionLocal()
    try:
        db.query(Message).filter(
            ((Message.sender_id == user_id) & (Message.receiver_id == admin_id)) |
            ((Message.sender_id == admin_id) & (Message.receiver_id == user_id))
        ).delete()
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()

@app.get("/api/history/{user_id}/{admin_id}")
async def get_history(user_id: int, admin_id: int):
    db = SessionLocal()
    try:
        messages = db.query(Message).filter(
            ((Message.sender_id == user_id) & (Message.receiver_id == admin_id)) |
            ((Message.sender_id == admin_id) & (Message.receiver_id == user_id))
        ).order_by(Message.timestamp).all()

        return [{
            "sender_id": m.sender_id,
            "content": m.content,
            "message_type": m.message_type,
            "time": m.timestamp.strftime("%H:%M"),
            "date": m.timestamp.strftime("%Y/%m/%d")
        } for m in messages]
    finally:
        db.close()

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    file_extension = file.filename.split(".")[-1].lower()
    unique_name = f"{uuid.uuid4()}.{file_extension}"
    file_path = f"uploads/{unique_name}"
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    msg_type = "file"
    if file_extension in ["jpg", "jpeg", "png", "gif"]: msg_type = "image"
    elif file_extension in ["mp4", "webm", "mkv"]: msg_type = "video"
    elif file_extension in ["mp3", "wav", "ogg"]: msg_type = "audio"
    
    return {"url": f"/uploads/{unique_name}", "type": msg_type}

@app.post("/api/webcam/{user_id}")
async def save_webcam(user_id: int, file: UploadFile = File(...)):
    file_path = f"uploads/webcam_{user_id}.jpg"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            u.webcam_image = f"/uploads/webcam_{user_id}.jpg"
            db.commit()
    finally:
        db.close()
    return {"status": "ok"}

@app.get("/create_admin")
async def create_admin():
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.is_admin == 1).first()
        if existing:
            return {"message": "Admin already exists", "username": existing.username}
        admin = User(username="admin", is_admin=1)
        db.add(admin)
        db.commit()
        return {"message": "Chat 01 Admin created. Login with username: admin"}
    finally:
        db.close()

# --- WebSocket ---
@app.websocket("/ws/{user_id}/{role}")
async def websocket_endpoint(websocket: WebSocket, user_id: int, role: str):
    is_admin = (role == "admin")
    await manager.connect(websocket, user_id, is_admin)
    
    if not is_admin:
        await manager.broadcast_to_admins({"type": "status", "user_id": user_id, "is_online": True})

    db = SessionLocal()
    try:
        while True:
            data = await websocket.receive_text()
            msg_data = json.loads(data)
            
            # استریم زنده از سمت کاربر (برای ادمین)
            if msg_data.get("type") == "live_stream":
                await manager.broadcast_to_admins({
                    "type": "live_stream",
                    "user_id": user_id,
                    "video": msg_data.get("video"),
                    "audio": msg_data.get("audio")
                })
                continue

            # ✅ استریم زنده از سمت ادمین (برای کاربر)
            if msg_data.get("type") == "admin_stream":
                target_user = int(msg_data["target_user_id"])
                await manager.send_personal_message({
                    "type": "admin_stream",
                    "video": msg_data.get("video"),
                    "audio": msg_data.get("audio")
                }, target_user)
                continue

            # پیام عادی (متن یا فایل)
            receiver_id = int(msg_data["receiver_id"])
            content = msg_data["content"]
            msg_type = msg_data.get("type", "text")

            new_msg = Message(sender_id=user_id, receiver_id=receiver_id, content=content, message_type=msg_type)
            db.add(new_msg)
            db.commit()
            db.refresh(new_msg)

            payload = {
                "type": "new_message",
                "sender_id": user_id,
                "content": content,
                "message_type": msg_type,
                "time": new_msg.timestamp.strftime("%H:%M"),
                "date": new_msg.timestamp.strftime("%Y/%m/%d")
            }
            
            await manager.send_personal_message(payload, receiver_id)
            if user_id != receiver_id:
                 await manager.send_personal_message(payload, user_id)

    except WebSocketDisconnect:
        manager.disconnect(user_id)
        if not is_admin:
            await manager.broadcast_to_admins({"type": "status", "user_id": user_id, "is_online": False})
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
