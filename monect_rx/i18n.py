"""Tiny TR/EN string table for the desktop UI."""
from __future__ import annotations


S = {
    "app": ("Monect4Linux", "Monect4Linux"),
    "tab_status": ("Durum", "Status"),
    "tab_security": ("Güvenlik", "Security"),
    "tab_rd": ("Uzak masaüstü", "Remote desktop"),
    "tab_help": ("Yardım", "Help"),
    "connecting_svc": ("Alıcı servisine bağlanılıyor…", "Connecting to the receiver service…"),
    "svc_ok": ("● Alıcı çalışıyor – telefon bağlanmaya hazır", "● Receiver running – ready for the phone"),
    "svc_down": ("● Alıcı servisi çalışmıyor (systemctl --user start monect-receiver)",
                 "● Receiver service is not running (systemctl --user start monect-receiver)"),
    "pc_name": ("Bilgisayar adı:", "Computer name:"),
    "save": ("Kaydet", "Save"),
    "ip": ("IP adresi:", "IP address:"),
    "language": ("Dil:", "Language:"),
    "autostart": ("Bilgisayar açılınca (oturum açılınca) otomatik başlat",
                  "Start automatically when I log in"),
    "devices": ("Bağlı cihazlar:", "Connected devices:"),
    "col_device": ("Cihaz", "Device"),
    "col_app": ("Uygulama", "App"),
    "col_state": ("Durum", "State"),
    "disconnect": ("Seçili cihazın bağlantısını kes", "Disconnect selected device"),
    "who": ("Kimler bağlanabilir?", "Who may connect?"),
    "sec_open": ("Aynı ağdaki her cihaz (şifresiz)", "Any device on this network (no password)"),
    "sec_confirm": ("Yeni cihaz bağlanırken bana sor", "Ask me when a new device connects"),
    "sec_password": ("Yeni cihazlar şifre girmeli", "New devices must enter a password"),
    "password": ("Şifre:", "Password:"),
    "pw_hint": ("değiştirmek için yeni şifre yaz", "type a new password to change it"),
    "remember": ("Bağlanan cihazları güvenilir olarak hatırla (bir daha sorma)",
                 "Remember connected devices as trusted (don't ask again)"),
    "apply": ("Uygula", "Apply"),
    "forget": ("Güvenilir cihazları unut", "Forget trusted devices"),
    "no_pw": ("Şifre modunu seçtin ama şifre girmedin.", "Password mode selected but no password set."),
    "quality": ("Görüntü kalitesi (bit hızı):", "Picture quality (bitrate):"),
    "max_fps": ("En yüksek kare hızı:", "Maximum frame rate:"),
    "rd_note": ("Düşük gecikme için: 5 GHz Wi‑Fi, 6–10 Mbit/s, 60 fps.\n"
                "Ağ yetmezse alıcı kareleri atlayıp gecikmeyi kendiliğinden düşük tutar.",
                "For low latency: 5 GHz Wi‑Fi, 6–10 Mbit/s, 60 fps.\n"
                "If the network can't keep up, frames are skipped so latency stays low."),
    "rd_apply": ("Uygula (bir sonraki uzak masaüstü oturumunda)", "Apply (next remote desktop session)"),
    "st_connected": ("Bağlı", "Connected"),
    "st_connecting": ("Bağlanıyor", "Connecting"),
    "st_new": ("Eşleşti", "Paired"),
    "st_rd": (" · uzak masaüstü", " · remote desktop"),
    "waiting": ("Bağlantı bekleniyor", "Waiting for a connection"),
    "is_connected": ("bağlı", "connected"),
    "open_window": ("Pencereyi aç", "Open window"),
    "restart": ("Alıcıyı yeniden başlat", "Restart receiver"),
    "quit_ui": ("Arayüzden çık", "Quit UI"),
    "joined": ("bağlandı", "connected"),
    "left": ("ayrıldı", "disconnected"),
    "req_title": ("Bağlantı isteği", "Connection request"),
    "req_text": ("bu bilgisayara bağlanmak istiyor.", "wants to connect to this computer."),
    "allow": ("İzin ver", "Allow"),
    "deny": ("Reddet", "Deny"),
    "restart_lang": ("Dil değişikliği arayüz yeniden açılınca uygulanır.",
                     "The language change applies after reopening the UI."),
    "help_html": ("""
<h3>Bağlanma</h3><p>Telefonda <b>PC Remote</b> uygulamasını aç, bu bilgisayarı listeden seç.
Telefon ve PC aynı ağda olmalı (UDP 28451).</p>
<h3>3D jest / hareket (Wiimote gibi)</h3><p>Dolphin → Ayarlar → Kumanda → <i>Alternate Input
Sources</i> → DSU Client: <code>127.0.0.1:26760</code>. Cemu/Ryujinx'te de aynı cemuhook adresi.</p>
<h3>Telefon kamerası = web kamerası</h3><p>Bir kez: <code>monect4linux-setup --webcam</code>
(sudo ister). Sonra uygulamada sanal kamerayı başlat, programlarda "Monect Camera" görünür.</p>
<h3>Data cable</h3><p>Telefonun gösterdiği <code>ftp://…:28456</code> adresini Dolphin'in adres çubuğuna yaz.</p>
<h3>Servis</h3><p><code>systemctl --user status monect-receiver</code> ·
<code>journalctl --user -u monect-receiver -f</code></p>""", """
<h3>Connecting</h3><p>Open the <b>PC Remote</b> app on the phone and pick this computer.
Phone and PC must be on the same network (UDP 28451).</p>
<h3>3D gesture / motion (Wiimote-like)</h3><p>Dolphin → Options → Controller Settings →
<i>Alternate Input Sources</i> → DSU Client: <code>127.0.0.1:26760</code>. Same cemuhook address
for Cemu/Ryujinx.</p>
<h3>Phone camera as webcam</h3><p>Once: <code>monect4linux-setup --webcam</code> (asks for sudo).
Then start the virtual camera in the app; "Monect Camera" appears in your programs.</p>
<h3>Data cable</h3><p>Type the <code>ftp://…:28456</code> address the phone shows into Dolphin.</p>
<h3>Service</h3><p><code>systemctl --user status monect-receiver</code> ·
<code>journalctl --user -u monect-receiver -f</code></p>"""),
}

LANG = 0


def detect(pref: str | None = None) -> None:
    global LANG
    if pref in ("tr", "en"):
        LANG = 0 if pref == "tr" else 1
        return
    LANG = 1                      # default: English (Turkish selectable in the UI)


def t(key: str) -> str:
    v = S.get(key)
    return v[LANG] if v else key
