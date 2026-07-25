# SoapDesk

Hafif masaüstü WSDL/SOAP test istemcisi. SoapUI'nin yaptığı işi, Java ve Electron
olmadan yapar: WSDL adresini aç, operation listesini gör, hazır envelope
şablonunu al, düzenle, gönder.

Tek dosya Python + tkinter. Windows için tek `.exe` olarak da dağıtılıyor.

## Özellikler

- **WSDL yükleme** — `zeep` ile, `strict=False` sayesinde mükerrer global element
  gibi bozuk WSDL'leri de tolere eder. SSL doğrulaması açılıp kapatılabilir.
- **Operation ağacı** — service / port / operation hiyerarşisi.
- **SoapUI tarzı envelope şablonu** — complex type'ları özyinelemeli açar,
  attribute'ları `?` ile işaretler, `Optional:` / `Zero or more repetitions:` /
  `You have a CHOICE of the next N items` yorumlarını basar. SOAP 1.1 ve 1.2
  namespace'lerini binding'e göre seçer, SOAP header tanımlıysa onu da üretir.
  Document stili olmayan binding'lerde (rpc/encoded) zeep'in kendi mesajına düşer.
- **Endpoint + header otomatik** — `SOAPAction` (1.1) veya `Content-Type` içinde
  `action=` (1.2) hazır gelir; ikisi de elle düzenlenebilir.
- **Taslaklar** — operation'lar arasında gezerken editördeki hâlin kaybolmaz;
  uygulama kapanırken diske yazılır, sonraki açılışta aynı WSDL için geri gelir.
- **Geçmiş** — gönderilen her istek (endpoint, header, envelope, cevap, HTTP kodu,
  süre) tek bir XML dosyasına kaydedilir. Çift tıkla geri yükle. Son 1000 kayıt
  tutulur; geçmiş dosyasının yeri değiştirilebilir.
- **Pretty-print cevap**, XML kaydet/aç, `Ctrl+Enter` ile gönder.

## Kurulum

### Hazır exe (Windows)

[Releases](../../releases/latest) sayfasından `SoapDesk.exe` indirilip
doğrudan çalıştırılır. Python kurulumu gerekmez.

### Kaynaktan

```bash
pip install -r requirements.txt
python soapdesk.py
```

Windows'ta python.org kurulumu tkinter'i içerir, ek bir şey gerekmez.
Linux'ta gerekirse: `apt install python3-tk`

## Kullanım

1. Üstteki alana WSDL adresini yaz, **Yükle**. Adres `?wsdl` ile bitmiyorsa
   eklemeyi teklif eder.
2. Soldaki ağaçtan bir operation seç — envelope, endpoint ve header'lar hazır gelir.
3. `?` yerlerini doldur, **Gönder** (veya `Ctrl+Enter`).
4. Cevap alt panelde; istek geçmiş listesine düşer.

**Şablonu yenile** düğmesi o operation için yaptığın düzenlemeleri atıp
sıfırdan şablon üretir.

## Dosyalar

| Yol | İçerik |
| --- | --- |
| `~/.soapdesk.json` | son WSDL adresi, SSL tercihi, geçmiş dosyasının yolu |
| `~/soapdesk_history.xml` | gönderilen istekler ve taslaklar |

## Exe derleme

```bash
pip install pyinstaller
pyinstaller SoapDesk.spec
```

Çıktı: `dist/SoapDesk.exe` (tek dosya, konsolsuz).
