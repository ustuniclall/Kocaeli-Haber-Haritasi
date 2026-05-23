import sys

from flask import Flask, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from flask import render_template
import subprocess
import os
from flask import send_from_directory
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def ana_sayfa():
    return render_template('index.html')
CORS(app) # Frontend'in bu verilere erişebilmesi için GÜVENLİK İZNİ

# MongoDB Bağlantı Bilgilerin
connection_string = "mongodb+srv://webscraping_db_user:654321.@cluster0.bdr2clt.mongodb.net/?appName=Cluster0"
client = MongoClient(connection_string)
db = client['KocaeliHaberSistemi']
collection = db['Haberler']

@app.route('/api/haberler', methods=['GET'])
def haberleri_getir():
    # Veritabanındaki tüm haberleri çek, MongoDB'nin özel _id alanını JSON hatası vermemesi için çıkar
    haberler = list(collection.find({}, {"_id": 0})) 
    return jsonify(haberler)

@app.route('/api/tara', methods=['GET'])
def haberleri_tara():
    try:
        # 1. ESKİ LOGLARI ANINDA TEMİZLE (Kritik Adım)
        log_file = "scraper.log"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] --- Sistem Başlatılıyor ---\n")
        
        # 2. Scraper'ı başlat
        # Windows kullanıyorsan "python" yerine "python.exe" gerekebilir
        subprocess.Popen([sys.executable, "scraper.py"]) 
        
        return jsonify({"status": "success", "message": "Tarama başlatıldı."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
 
@app.route('/api/logs')
def get_logs():
    log_file = "scraper.log"
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            # Son 10 satırı gönderiyoruz ki kullanıcı akışı görsün
            return jsonify(lines[-10:])
    return jsonify(["Log dosyası bekleniyor..."])


if __name__ == '__main__':
    print("🚀 Flask Sunucusu 5000 portunda başlatılıyor...")
    # use_reloader=False hatayı engelleyecektir
    app.run(debug=True, port=5000, use_reloader=False)