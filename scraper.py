import random

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
import re
from datetime import datetime, timedelta
from geopy.geocoders import Nominatim
import time
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from dateutil import parser as dateparser
from sentence_transformers import SentenceTransformer, util
import torch # Benzerlik hesaplaması için gerekli
import os
from dotenv import load_dotenv
import cloudscraper

load_dotenv() # .env dosyasındaki verileri yükler
api_key = os.getenv("GOOGLE_MAPS_API_KEY")

# Google Geocoding kullanacaksan:
from geopy.geocoders import GoogleV3
geolocator = GoogleV3(api_key=api_key)

# Çok dilli (Türkçe destekli) hafif bir model yüklüyoruz
model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')


# ----------------------------
# MongoDB bağlantı
# ----------------------------
connection_string = "mongodb+srv://webscraping_db_user:654321.@cluster0.bdr2clt.mongodb.net/?appName=Cluster0"

client = MongoClient(connection_string)

db = client["KocaeliHaberSistemi"]
collection = db["Haberler"]

geolocator = Nominatim(user_agent="kocaeli_haber_sistemi")


# scraper.py dosyasının en üstlerine ekle
def log_yaz(mesaj):
    zaman = datetime.now().strftime("%H:%M:%S")
    tam_mesaj = f"[{zaman}] {mesaj}"
    print(tam_mesaj)
    with open("scraper.log", "a", encoding="utf-8") as f:
        f.write(tam_mesaj + "\n")

# ----------------------------
# METİN TEMİZLEME
# ----------------------------
def metin_temizle(ham_metin):
    if not ham_metin:
        return ""

    temiz = re.sub(r'<(script|style).*?>.*?</\1>', '', ham_metin, flags=re.DOTALL)
    temiz = re.sub(r'<.*?>', ' ', temiz)

    # 🔥 REKLAM TEMİZLE
    reklam_kelimeler = ["reklam","sponsor","banner","kampanya","şenliği","detaylar","www","444","0850"]
    for k in reklam_kelimeler:
        temiz = re.sub(k, '', temiz, flags=re.IGNORECASE)

    temiz = re.sub(r'[^\w\s\d.,!?\-çğıöşü]', '', temiz)
    temiz = temiz.lower()

    return re.sub(r'\s+', ' ', temiz).strip()


# ----------------------------
# TARİH PARSE
# ----------------------------
def tarih_parse_et(tarih_metni):
    if not tarih_metni:
        return None

    tarih_metni = tarih_metni.lower().strip()

    ay_map = {
    "ocak":"jan","oca":"jan",
    "şubat":"feb","sub":"feb",
    "mart":"mar",
    "nisan":"apr","nis":"apr",
    "mayıs":"may","may":"may",
    "haziran":"jun","haz":"jun",
    "temmuz":"jul","tem":"jul",
    "ağustos":"aug","agu":"aug",
    "eylül":"sep","eyl":"sep",
    "ekim":"oct","eki":"oct",
    "kasım":"nov","kas":"nov",
    "aralık":"dec","ara":"dec"
    }

    for tr,en in ay_map.items():
        tarih_metni = tarih_metni.replace(tr,en)

    tarih_metni = re.sub(r'güncelleme:', '', tarih_metni)

    try:
        dt = dateparser.parse(tarih_metni, dayfirst=True)

        if not dt:
            return None

        dt = dt.replace(tzinfo=None)

        # 🔥 SADECE GELECEK TARİHLERİ ENGELLE
        if dt > datetime.now():
            return None

        return dt  # 🔥 HER ZAMAN GERÇEK TARİHİ DÖN

    except:
        return None
    
# Dosyanın üst kısmına boş bir sözlük ekle
konum_cache = {}

def koordinat_al(konum):
    # 1. Önce hafızaya (Cache) bak
    if konum in konum_cache:
        print(f"📍 {konum} için cache kullanıldı.")
        return konum_cache[konum]

    # 2. Cache'de yoksa API'ye sor
    try:
        # Örnek GoogleV3 kullanımı (veya Nominatim ile devam edebilirsin)
        loc = geolocator.geocode(f"{konum}, Kocaeli, Turkey")
        if loc:
            res = {"lat": loc.latitude, "lng": loc.longitude}
            # Bulunan sonucu cache'e kaydet
            konum_cache[konum] = res 
            return res
    except Exception as e:
        print("Geocoding hatası:", e)
    
    return None

# ----------------------------
# HABER ANALİZİ
# ----------------------------
def haber_analiz_ve_konum_bul(baslik, icerik):
    metin_full = (baslik + " " + icerik).lower()
    
    metin_lower = metin_full.replace("i̇", "i").replace("’", " ").replace("'", " ")
    # İlçe isimlerindeki 'ı' harflerini 'i' yaparak eşleşmeyi garantiye alıyoruz
    metin_lower = metin_lower.replace("ı", "i")    # 1. TÜR SINIFLANDIRMA (Sıkı Denetim)
    # PDF Madde 2: Gelişmiş ve Spesifik Sınıflandırma
    kategoriler = [
        ("Yangın", [r'yangın', r'itfaiye', r'alevler', r'orman yangını', r'yanıyor', r'duman']),
        ("Cinayet & Saldırı", [r'cinayet', r'öldürüldü', r'\bbıçakla', r'silahlı saldırı', r'tabancayla', r'vuruldu', r'istismar', r'silahlı', r'katliam', r'ölüm']),
        ("Trafik Kazası", [r'trafik kazası', r'çarpıştı', r'yaralı', r'zincirleme kaza', r'devrildi']),
        ("Asayiş & Operasyon", [r'gözaltı', r'tutuklandı', r'polis operasyonu', r'şüpheli', r'jandarma', r'asayiş']),
        ("Hırsızlık & Dolandırıcılık", [r'hırsızlık', r'soygun', r'dolandırıcılık', r'dolandırıcı', r'vurgun', r'gaspedildi', r'gasp']),
        ("Spor", [r'kocaelispor', r'kağıtspor', r'maç sonucu', r'stadyum', r'transfer', r'şampiyonluk', r'potada',r'\bspor\b']),
        ("Elektrik Kesintisi", [r'elektrik kesintisi', r'sedaş', r'sepaş', r'enerji kesintisi',r'elektrik']),
        ("Kültürel Etkinlikler", [r'konser', r'festival', r'tiyatro', r'sergi', r'iftar programı', r'miting'])
    ]
    
    tur = None
    for k, patterns in kategoriler:
        for p in patterns:
            if re.search(p, metin_lower):
                tur = k
                break
        if tur: break
    
    if not tur: return None, None

    # 2. İLÇE TESPİTİ (Gelişmiş Eşleşme)
    ilceler_map = {
        "izmit": "Izmit", "gebze": "Gebze", "golcuk": "Gölcük", 
        "korfez": "Körfez", "derince": "Derince", "kartepe": "Kartepe", 
        "darica": "Darıca", "kandira": "Kandıra", "karamursel": "Karamürsel", 
        "basiskele": "Başiskele", "dilovasi": "Dilovası", "cayirova": "Çayırova"
    }
    
    tespit_edilen_ilce = None
    for anahtar, orjinal in ilceler_map.items():
        if anahtar in metin_lower: 
            tespit_edilen_ilce = orjinal
            break
            
    if not tespit_edilen_ilce: 
        print(f"DEBUG - Konum aranan metin: {metin_lower[:100]}")
        return None, None
    
    # 3. SPESİFİK KONUM TESPİTİ (Hiyerarşik Arama)
    # Özel Bina/Nokta
    ozel_nokta = re.search(r'([A-ZÇĞİÖŞÜ][a-zçğıöşü]+\s+(Adliyesi|Hastanesi|Emniyet|Karakolu|Terminali|Camisi|Mevkii|Köyü|Parkı|Meydanı|Köprüsü))', metin_full)
    
    # Sokak veya Cadde (Gelişmiş Regex: "Hürriyet Caddesi" veya "Okul Sokak" gibi)
    # [A-ZÇĞİÖŞÜa-zçğıöşü]+ ile Türkçe karakterli ve çok kelimeli isimleri yakalar
    cadde_sokak_bul = re.search(r'(([A-ZÇĞİÖŞÜa-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜa-zçğıöşü]+)*)\s+(Caddesi|Sokağı|Sokak|Bulvarı|Yolu))', metin_full, re.IGNORECASE)
    
    # Mahalle
    mah_bul = re.search(r'(([A-ZÇĞİÖŞÜa-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜa-zçğıöşü]+)*)\s+Mahallesi)', metin_full, re.IGNORECASE)
    
    # Karayolu ve Otoyol isimlerini yakalamak için:
    # haber_analiz_ve_konum_bul fonksiyonu içindeki 3. ADIM:
    # Önce en spesifik olan D-100 gibi isimleri yakala
    ana_yollar = re.search(r'((D-100|E-5|Otoyolu)\s*(Karayolu|Üzeri|Mevkii)?)', metin_full, re.IGNORECASE)

    # Eğer spesifik bir yol ismi yoksa genel "Karayolu" kelimesine bak
    if not ana_yollar:
        ana_yollar = re.search(r'([A-ZÇĞİÖŞÜa-zçğıöşü]+\s+(Karayolu|Yolu))', metin_full, re.IGNORECASE)
    
    adres = []
    if ozel_nokta: 
        adres.append(ozel_nokta.group(1))
    if cadde_sokak_bul: 
        adres.append(cadde_sokak_bul.group(1).title())
    if mah_bul: 
        adres.append(mah_bul.group(1).title())
    if ana_yollar:
        adres.append(ana_yollar.group(1).title())
    
    adres.append(tespit_edilen_ilce.title())
    
    # dict.fromkeys ile tekrar eden kelimeleri temizleyip birleştiriyoruz
    final_konum = ", ".join(dict.fromkeys(adres))
    return tur, final_konum

def benzerlik_kontrol_ve_kaynak_yonetimi(yeni_haber):
    """
    %90 embedding benzerliği ile haber tekilleştirme ve kaynak birleştirme yapar.
    """
    # Veritabanındaki son 48 saatlik haberleri inceleyelim
    zaman_siniri = datetime.now() - timedelta(days=3)
    mevcut_haberler = list(collection.find({"tarih_obj": {"$gte": zaman_siniri}}))
    
    if not mevcut_haberler:
        return False # Karşılaştıracak haber yoksa direkt kaydet

    # Yeni haberin içeriğini sayısallaştır (Embedding)
    yeni_emb = model.encode(yeni_haber['haber_icerigi'], convert_to_tensor=True)

    for eski_haber in mevcut_haberler:
        # Eski haberin embedding'ini al
        eski_emb = model.encode(eski_haber['haber_icerigi'], convert_to_tensor=True)
        
        # Kosinüs Benzerliği hesapla
        benzerlik_orani = util.cos_sim(yeni_emb, eski_emb).item()
        
        if benzerlik_orani >= 0.90:
            print(f"🎯 Benzer Haber Tespiti (%{benzerlik_orani*100:.2f}): {yeni_haber['kaynak_adi']} -> {eski_haber['kaynak_adi']}")
            
            # Kaynak zaten listede yoksa ekle (addToSet listede benzersiz tutar)
            collection.update_one(
                {"_id": eski_haber["_id"]},
                {"$addToSet": {"kaynak_adi":{"$each": yeni_haber['kaynak_adi']}}}
            )
            return True # Benzer bulundu, yeni döküman oluşturma

    return False # Benzer bulunamadı

# ----------------------------gercek_tarih
# HABER TOPLAMA
# ----------------------------
def haberleri_topla():
    # Dosyayı her tarama başında sıfırla
    with open("scraper.log", "w", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] --- Tarama Başlatıldı ---\n")
        
    requests.packages.urllib3.disable_warnings()    

    kaynaklar = [
        {
            "ad": "Yeni Kocaeli", 
            "url": "https://www.yenikocaeli.com/", 
            "timeout": 35,
            "verify": True,
            "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.google.com/",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
            }
        },
        {
            "ad": "Çağdaş Kocaeli", 
            "url": "https://www.cagdaskocaeli.com.tr/", 
            "timeout": 15, 
            "verify": True,
            "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"}
        },
        {
            "ad": "Özgür Kocaeli", 
            "url": "https://www.ozgurkocaeli.com.tr/", 
            "timeout": 20, 
            "verify": True,
            "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"}
        },
        {
            "ad": "Ses Kocaeli", 
            "url": "https://www.seskocaeli.com/", 
            "timeout": 20, 
            "verify": True,
            "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"}
        },
        {
            "ad": "Bizim Yaka", 
            "url": "https://www.bizimyaka.com/", 
            "timeout": 20, 
            "verify": True,
            "headers": {"User-Agent": "Mozilla/5.0"}
        }
    ]

    scraper = cloudscraper.create_scraper()
    for kaynak in kaynaklar:
        log_yaz(f"--- {kaynak['ad']} taranıyor ---")
        
        try:
            # Sitenin kendi özel ayarlarıyla ana sayfayı çek
            res = scraper.get(
                kaynak["url"], 
                headers=kaynak["headers"], 
                timeout=kaynak["timeout"], 
                verify=kaynak["verify"]
            )
            res.raise_for_status()
            
            soup = BeautifulSoup(res.content, "html.parser")

            links = soup.select("h1 a, h2 a, h3 a, article a")
            count = 0

            for a in links:
                if count >= 10:
                    break

                url = a.get("href")
                if not url: continue

                if not url.startswith("http"):
                    url = kaynak["url"].rstrip("/") + "/" + url.lstrip("/")

                if collection.find_one({"link": url}):
                    continue

                # 🔥 DETAY SAYFASI İÇİN ÖZEL AYARLAR
                # Her haber arasında rastgele bekleme yaparak "İnsansı" davran
                time.sleep(random.uniform(2, 4)) 

                try:
                    detay = requests.get(
                        url, 
                        headers=kaynak["headers"], 
                        timeout=kaynak["timeout"], 
                        verify=kaynak["verify"]
                    )
                    detay_soup = BeautifulSoup(detay.content, "html.parser")
                except Exception as e:
                    log_yaz(f"⚠️ Detay sayfası atlandı ({kaynak['ad']}): {url}")
                    continue

                for gereksiz in detay_soup(['script','style','footer','nav','aside','iframe']):
                    gereksiz.decompose()

                for bozuk in detay_soup.find_all(
                    attrs={"class": re.compile('comment|reply|reklam|ads|banner|sidebar|widget|related|benzer|populer|oneri', re.I),
                           "id": re.compile(r'sidebar|reklam|ads|related', re.I)}):
                    bozuk.decompose()

                icerik_div = detay_soup.find(['div','article'], class_=['haber_metni','content','post-content','text-content','articleBody'])

                if icerik_div:
                    ham_metin = icerik_div.get_text(" ", strip=True)
                else:
                    ham_metin = " ".join([p.get_text() for p in detay_soup.find_all('p') if len(p.get_text()) > 20])

                if len(ham_metin) < 150:
                    continue

                temiz_metin = metin_temizle(ham_metin)

                # Tarih bulma işlemleri...
                tarih_text = None
                meta_tags = [{"property": "article:published_time"}, {"name": "publish_date"}, {"property": "og:published_time"}]
                for tag in meta_tags:
                    meta = detay_soup.find("meta", tag)
                    if meta:
                        tarih_text = meta.get("content")
                        break

                if not tarih_text:
                    tarih_elementi = detay_soup.find(['span', 'div', 'time'], class_=['date', 'time', 'post-date', 'haber-tarihi'])
                    if tarih_elementi:
                        tarih_text = tarih_elementi.get_text(strip=True)

                if not tarih_text:
                    match = re.search(r'\d{1,2}[.\/\s]+[A-Za-z0-9çğıöşü]+[.\/\s]+\d{4}', ham_metin)
                    if match:
                        tarih_text = match.group()

                gercek_tarih = tarih_parse_et(tarih_text)
                if gercek_tarih is None:
                    log_yaz(f"⏩ Tarih eski/hatalı ({tarih_text}): {url}")
                    continue
                
                # 🔥 ASIL FİLTRE BURADA OLMALI
                uc_gun_once = datetime.now() - timedelta(days=3)

                if gercek_tarih < uc_gun_once:
                    log_yaz(f"⏩ Eski haber atlandı ({gercek_tarih}): {url}")
                    continue

                tur, konum = haber_analiz_ve_konum_bul(a.text, temiz_metin)
                if not tur:
                    log_yaz(f"🔎 Tür belirlenemedi: {a.text[:50]}...")
                    continue
                if not konum:
                    log_yaz(f"📍 Konum (ilçe) bulunamadı: {a.text[:50]}...")
                    continue

                coords = koordinat_al(konum)
                if not coords:
                    continue

                # Konum Doğrulama
                if not any(ilce in konum.lower() for ilce in ["izmit","gebze","gölcük","körfez","derince","kartepe","darica","kandira","karamürsel","başiskele","dilovasi","çayirova"]):
                    continue

                veri = {
                    "haber_turu": tur,
                    "haber_basligi": a.text.strip(),
                    "haber_icerigi": temiz_metin,
                    "konum_metin": konum,
                    "kaynak_adi": [kaynak["ad"]],
                    "link": url,
                    "yayin_tarihi": gercek_tarih.strftime("%d-%m-%Y"),
                    "tarih_obj": gercek_tarih,
                    "koordinat": coords
                }

                if benzerlik_kontrol_ve_kaynak_yonetimi(veri):
                    log_yaz("🎯 Benzer haber bulundu, kaynak listesi güncellendi.")
                else:
                    collection.insert_one(veri)
                    log_yaz(f"✅ YENİ: [{veri['haber_turu']}] - {veri['konum_metin']}")

                count += 1

        except Exception as e:
            log_yaz(f"❌ {kaynak['ad']} Hatası: {str(e)}")
            
    log_yaz("✅ Tarama Bitti")   

if __name__=="__main__":
    # Tüm haberleri silmek yerine (delete_many({})), sadece 3 günden eskileri sil
    uc_gun_once = datetime.now() - timedelta(days=3)
    silme_sonucu = collection.delete_many({"tarih_obj": {"$lt": uc_gun_once}})
    print(f"🧹 {silme_sonucu.deleted_count} adet eski haber temizlendi.")
    haberleri_topla()