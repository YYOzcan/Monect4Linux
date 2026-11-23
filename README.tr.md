# Monect4Linux

**Monect *PC Remote* Android uygulaması için Linux alıcısı.** Telefonunu Linux
bilgisayarda touchpad, klavye, oyun kolu, hareket kumandası, uzak ekran ve daha
fazlası olarak kullan. Windows'taki "PC Remote Receiver"ın sıfırdan yazılmış Linux karşılığı.

[English](README.md)

> Resmi değildir; Monect ile bağlantısı yoktur, Monect'e ait kod veya dosya içermez.

## Kurulum (tek komut)

```bash
curl -fsSL https://raw.githubusercontent.com/YYOzcan/Monect4Linux/main/install.sh | bash
```

Kurulum dağıtımını tanır (Arch/CachyOS/Manjaro, Debian/Ubuntu/Mint, Fedora,
openSUSE), yalnızca eksik paketleri kurar (bir kez şifre sorar), sanal giriş
aygıtlarına izin verir, güvenlik duvarı açıksa yerel ağa izin verir; alıcı ve
tepsi uygulaması her oturum açılışında başlar.

| Komut | |
|---|---|
| `monect4linux-setup --webcam` | telefon kamerasını web kamerası yap |
| `monect4linux-setup --update` | güncelle |
| `monect4linux-uninstall [--purge]` | kaldır |

## Kullanım

1. Telefon ve PC aynı ağda olsun.
2. Telefonda **PC Remote**'u aç, bilgisayarını seç.
3. İlk uzak masaüstünde ekran paylaşım izni bir kez sorulur.

**Dolphin'de hareket kontrolü:** Ayarlar → Kumanda → Alternate Input Sources →
DSU Client → `127.0.0.1:26760`.

Dil (Türkçe/İngilizce), güvenlik, görüntü kalitesi ve otomatik başlatma tepsideki
**Monect4Linux** uygulamasından ayarlanır.

Özellik listesi ve sorun giderme için: [README.md](README.md)
