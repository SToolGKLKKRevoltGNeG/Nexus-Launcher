"""
NexusGames - Desktop WebView Launcher
Mo cua so desktop (webview) hien thi website https://steam-snowy.vercel.app/
Khong co thanh dia chi, giong app desktop. Inject token de unlock web (gate).
Auto can bang kich thuoc cua so theo moi do phan giai / may (dung workarea).

NexusAPI: bridge Python cho JS (window.pywebview.api) de check Steam, cai/share NexusT.
Logic adapt tu Steam Project\Steam.py (registry, gdown, pyzipper, merge_and_replace).

FIX: An console SAU khi import OK. Neu thieu deps (webview/gdown/pyzipper),
giu console + in loi ro + pause de user biet cai gi thieu (debug duoc).
"""
import ctypes
import sys

# Kiem tra deps TRUOC khi an console. Neu fail -> in loi + pause -> user thay.
_MISSING = []
try:
    import secrets
    import threading
    import os
    import shutil
    import zipfile
    import traceback
    import subprocess
    import time
except ImportError as e:
    _MISSING.append(("stdlib", str(e)))

try:
    import webview
except ImportError as e:
    _MISSING.append(("pywebview", str(e)))

# gdown, pyzipper la optional o day (chi can khi user bấm nút) -> import lazy trong method.
# Nhung check som de bao user truoc, khong phai den khi bam nút moi loi.
try:
    import gdown
except ImportError:
    _MISSING.append(("gdown", "pip install gdown"))
try:
    import pyzipper
except ImportError:
    _MISSING.append(("pyzipper", "pip install pyzipper"))

if _MISSING:
    print("=" * 60)
    print("NexusGames: THIEU DEPENDENCIES")
    print("=" * 60)
    for pkg, hint in _MISSING:
        print(f"  - {pkg}: {hint}")
    print()
    print("Cach fix: Chay file install.bat hoac:")
    print("  pip install -r requirements.txt")
    print("=" * 60)
    try:
        input("Nhan Enter de thoat...")
    except Exception:
        pass
    sys.exit(1)

# Tat ca deps OK -> an console (app chay binh thuong).
try:
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass


# Bridge Python<->JS: JS goi await window.pywebview.api.method(args) -> Python chay -> tra dict.
# pywebview tu dispatch thread rieng -> UI khong freeze.
class NexusAPI:
    """API expose cho JS qua pywebview js_api. Method tra dict (JSON-serializable)."""

    # ---- Link/password (cai NexusT vs share game khac nhau) ----
    NEXUST_FILE_ID = "1Px5UNjTSndsvnXCtcaHmCYc3g1C0OM60"
    NEXUST_ZIP_PW = b"p1V8rYc6D4QnMPKr1j2KRGC70Sstkya95dLYseR0XXEvUdtkoT"

    LUA_FILE_ID = "1TKAB4zanGeF6du7ldxX7LIFhAOKmbVW8"
    LUA_ZIP_PW = b"93yee1KAbAaPRKySPZ2Fzb6OOBNgqwW87bQCFz"

    # ---- Helpers ----
    def _get_steam_install_path(self):
        # Doc registry HKLM\SOFTWARE\WOW6432Node\Valve\Steam\InstallPath (fallback Software\Valve\Steam)
        try:
            import winreg
            for key_path in (r"SOFTWARE\WOW6432Node\Valve\Steam", r"SOFTWARE\Valve\Steam"):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                        install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                        return install_path
                except OSError:
                    continue
        except Exception:
            return None
        return None

    def _prepare_hideout(self):
        # Tao thu muc an NexusHideout trong %APPDATA% (pattern giong Steam.py).
        appdata = os.getenv('APPDATA') or os.path.expanduser("~")
        hidden_dir = os.path.join(appdata, "NexusHideout")
        os.makedirs(hidden_dir, exist_ok=True)
        try:
            FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(hidden_dir, FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass
        return hidden_dir

    def _cleanup_hideout(self, hidden_dir):
        # Xoa NexusHideout + reset attribute hidden.
        try:
            FILE_ATTRIBUTE_NORMAL = 0x80
            ctypes.windll.kernel32.SetFileAttributesW(hidden_dir, FILE_ATTRIBUTE_NORMAL)
        except Exception:
            pass
        try:
            shutil.rmtree(hidden_dir, ignore_errors=True)
        except Exception:
            pass

    def _download_zip(self, hidden_dir, zip_filename, file_id):
        """Download file tu Google Drive ve hidden_dir.
        Dung drive.usercontent.google.com (endpoint download truc tiep, ~10MB/s)
        thay vi drive.google.com/uc (bGoogle throttle non-browser -> 78KB/s -> 15ph cho 70MB).
        Xu ly confirm page (file >100MB Google tra HTML -> parse confirm+uuid -> retry).
        Return zip_path hoac raise Exception."""
        try:
            import requests
        except ImportError:
            raise Exception("Thiếu thư viện requests. Cài: pip install requests")
        zip_path = os.path.join(hidden_dir, zip_filename)
        # Endpoint download truc tiep (khong bi throttle nhu /uc).
        base = "https://drive.usercontent.google.com/download"
        params = {"id": file_id, "export": "download", "confirm": "t"}
        # Desktop UA.
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
        sess = requests.Session()
        last_err = None
        for attempt in range(1, 4):  # 3 lan retry.
            try:
                r = sess.get(base, params=params, stream=True, timeout=30, headers=headers)
                # File >100MB hoac flagged -> Google tra trang HTML confirm page (chu khong phai file).
                ct = (r.headers.get("content-type") or "").lower()
                cd = (r.headers.get("content-disposition") or "").lower()
                if "text/html" in ct or "attachment" not in cd:
                    # La confirm page -> parse confirm + uuid, retry voi token.
                    import re
                    body = r.text
                    r.close()
                    # Confirm token co the o form hoac JS. Lay ca 2 pattern.
                    m_conf = re.search(r'confirm=([0-9A-Za-z_-]+)', body)
                    m_uuid = re.search(r'name="uuid"\s+value="([^"]+)"', body) \
                             or re.search(r'"uuid":"([^"]+)"', body)
                    if m_conf:
                        p2 = dict(params)
                        p2["confirm"] = m_conf.group(1)
                        if m_uuid:
                            p2["uuid"] = m_uuid.group(1)
                        r = sess.get(base, params=p2, stream=True, timeout=30, headers=headers)
                    else:
                        raise Exception("Google Drive tra confirm page nhung khong parse duoc token")
                total = int(r.headers.get("Content-Length", 0) or 0)
                r.raise_for_status()
                # Ghi voi chunk 8MB -> giam overhead, toi da toc do.
                with open(zip_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)
                r.close()
                if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
                    if total and os.path.getsize(zip_path) != total:
                        raise Exception(f"File khong day du: {os.path.getsize(zip_path)}/{total} bytes")
                    return zip_path
                raise Exception("File tai ve bi rong")
            except Exception as e:
                last_err = e
                try:
                    if os.path.exists(zip_path):
                        os.remove(zip_path)
                except Exception:
                    pass
                time.sleep(1.5 * attempt)  # backoff.
        raise Exception(f"Download that bai sau 3 lan: {last_err}")

    def _extract_zip(self, zip_path, extract_dir, pwd):
        # pyzipper AES extract. Return extract_dir (da tao).
        try:
            import pyzipper
        except ImportError as e:
            raise Exception("Thiếu thư viện pyzipper. Cài: pip install pyzipper")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)
        with pyzipper.AESZipFile(zip_path, 'r') as zip_ref:
            for file in zip_ref.namelist():
                zip_ref.extract(file, path=extract_dir, pwd=pwd)
        return extract_dir

    def _merge_and_replace_all(self, src_dir, dst_dir):
        # Move TOAN BO file/folder tu src -> dst (replace). Giong Steam.py merge_and_replace.
        for root, dirs, files in os.walk(src_dir):
            rel_path = os.path.relpath(root, src_dir)
            target_path = dst_dir if rel_path == '.' else os.path.join(dst_dir, rel_path)
            os.makedirs(target_path, exist_ok=True)
            for file in files:
                src_file = os.path.join(root, file)
                dst_file = os.path.join(target_path, file)
                if os.path.exists(dst_file):
                    os.remove(dst_file)
                shutil.move(src_file, dst_file)

    def _elevated_merge(self, src_dir, dst_dir):
        """Khi ghi vao dst_dir bi PermissionError (Steam o Program Files),
        spawn helper .bat voi quyen admin (UAC prompt) de copy bang robocopy.
        Tra True neu thanh cong, False neu user tu choi UAC / timeout / robocopy fail.
        Robocopy exit code < 8 = thanh cong (0=ko can copy, 1=copy xong, ...)."""
        import ctypes
        import time
        temp_dir = os.getenv('TEMP', os.path.expanduser('~'))
        flag_path = os.path.join(temp_dir, "_nx_merge_done.flag")
        bat_path = os.path.join(temp_dir, "_nx_merge.bat")
        # Xoa flag cu neu co.
        try:
            if os.path.exists(flag_path): os.remove(flag_path)
        except Exception:
            pass
        # .bat: robocopy /E (copy tat ca + overwrite), /R:2 /W:2 (2 retry x 2s, tranh hang),
        # quiet, ghi exit code vao flag. .bat tu xoa khong tin cay duoc (cmd giu file),
        # nen Python se xoa .bat sau khi doc flag (luc do .bat da thoat).
        with open(bat_path, "w") as f:
            f.write('@echo off\n')
            f.write(f'robocopy "{src_dir}" "{dst_dir}" /E /R:2 /W:2 /NFL /NDL /NJH /NJS /NP >nul 2>&1\n')
            f.write(f'echo %errorlevel% > "{flag_path}"\n')
        # Spawn voi "runas" verb = UAC prompt. SW_HIDE = 0 (an cua so).
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", bat_path, None, None, 0)
        if ret <= 32:
            # User tu choi UAC hoac loi.
            try: os.remove(bat_path)
            except Exception: pass
            return False
        # Poll cho flag file (timeout 300s — robocopy file lon co the cham).
        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(flag_path):
                try:
                    with open(flag_path) as ff:
                        code = int(ff.read().strip())
                    os.remove(flag_path)
                    # Robocopy: <8 = ok (0..7), >=8 = fail.
                    ok = code < 8
                except Exception:
                    try: os.remove(flag_path)
                    except Exception: pass
                    ok = False
                # Don dep .bat (da thoat, khong con bi lock).
                try: os.remove(bat_path)
                except Exception: pass
                return ok
            time.sleep(0.5)
        # Timeout — don dep .bat (co the van dang chay, xoa that bai khong sao).
        try: os.remove(bat_path)
        except Exception: pass
        return False

    def _elevated_copy_single(self, src_file, dst_dir):
        """Spawn admin .bat de copy 1 file vao dst_dir (cho share_game khi PermissionError).
        Tra True neu thanh cong (copy exit code 0)."""
        import ctypes
        import time
        temp_dir = os.getenv('TEMP', os.path.expanduser('~'))
        flag_path = os.path.join(temp_dir, "_nx_copy_done.flag")
        bat_path = os.path.join(temp_dir, "_nx_copy.bat")
        try:
            if os.path.exists(flag_path): os.remove(flag_path)
        except Exception:
            pass
        with open(bat_path, "w") as f:
            f.write('@echo off\n')
            f.write(f'if not exist "{dst_dir}" mkdir "{dst_dir}"\n')
            f.write(f'copy /Y "{src_file}" "{dst_dir}\\" >nul 2>&1\n')
            f.write(f'echo %errorlevel% > "{flag_path}"\n')
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", bat_path, None, None, 0)
        if ret <= 32:
            try: os.remove(bat_path)
            except Exception: pass
            return False
        deadline = time.time() + 60
        while time.time() < deadline:
            if os.path.exists(flag_path):
                try:
                    with open(flag_path) as ff:
                        code = int(ff.read().strip())
                    os.remove(flag_path)
                    ok = code == 0  # copy: 0 = ok.
                except Exception:
                    try: os.remove(flag_path)
                    except Exception: pass
                    ok = False
                try: os.remove(bat_path)
                except Exception: pass
                return ok
            time.sleep(0.5)
        try: os.remove(bat_path)
        except Exception: pass
        return False

    # ---- API methods (goi tu JS) ----
    def check_steam(self):
        """Kiem tra Steam co cai khong (doc registry). Tra {installed, path}."""
        try:
            path = self._get_steam_install_path()
            installed = bool(path) and os.path.isdir(path) if path else False
            return {"installed": installed, "path": path or ""}
        except Exception as e:
            return {"installed": False, "path": "", "error": str(e)}

    def check_nexust(self, steam_path):
        """Kiem tra {steam_path}\\opensteamtool\\Nexus co khong. Tra {installed}."""
        try:
            if not steam_path:
                return {"installed": False, "error": "Thiếu đường dẫn Steam"}
            nexus_dir = os.path.join(steam_path, "opensteamtool", "Nexus")
            return {"installed": os.path.isdir(nexus_dir)}
        except Exception as e:
            return {"installed": False, "error": str(e)}

    def install_nexust(self, steam_path):
        """Cai NexusT: gdown Nexus.zip -> giai nen -> move TOAN BO vao steam_path. Tra {success, error}.
        Neu ghi vao steam_path bi PermissionError (Steam o Program Files),
        tu spawn UAC prompt (robocopy xong) -> khong can user tu run as admin."""
        try:
            if not steam_path or not os.path.isdir(steam_path):
                return {"success": False, "error": "Thư mục Steam không tồn tại"}
            hidden_dir = self._prepare_hideout()
            try:
                zip_path = self._download_zip(hidden_dir, "Nexus.zip", self.NEXUST_FILE_ID)
                extract_dir = os.path.join(hidden_dir, "extracted_files")
                self._extract_zip(zip_path, extract_dir, self.NEXUST_ZIP_PW)
                # Xoa zip sau khi giai nen.
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                # Move TOAN BO file/folder vao steam_path (replace).
                # Neu PermissionError (Steam o Program Files) -> spawn UAC robocopy.
                try:
                    self._merge_and_replace_all(extract_dir, steam_path)
                except PermissionError:
                    ok = self._elevated_merge(extract_dir, steam_path)
                    if not ok:
                        return {"success": False,
                                "error": "Cần cấp quyền Administrator (UAC) để ghi vào thư mục Steam. Vui lòng bấm Yes khi hộp thoại UAC xuất hiện."}
            finally:
                # Luon cleanup NexusHideout du thanh cong hay fail.
                self._cleanup_hideout(hidden_dir)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}

    def share_game(self, steam_path, appid):
        """Share game: gdown Lua.zip -> giai nen -> move DUY NHAT {appid}.lua vao Nexus folder.
        Tra {success, already, error}.
        Neu {appid}.lua da co trong Nexus folder -> already=true (khong move)."""
        try:
            if not steam_path or not os.path.isdir(steam_path):
                return {"success": False, "error": "Thư mục Steam không tồn tại"}
            nexus_dir = os.path.join(steam_path, "opensteamtool", "Nexus")
            os.makedirs(nexus_dir, exist_ok=True)

            lua_filename = f"{appid}.lua"
            target_lua = os.path.join(nexus_dir, lua_filename)
            # Da co -> khong move.
            if os.path.exists(target_lua):
                return {"success": True, "already": True}

            hidden_dir = self._prepare_hideout()
            try:
                zip_path = self._download_zip(hidden_dir, "Lua.zip", self.LUA_FILE_ID)
                extract_dir = os.path.join(hidden_dir, "extracted_files")
                self._extract_zip(zip_path, extract_dir, self.LUA_ZIP_PW)
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                # Tim DUY NHAT {appid}.lua trong extract_dir (de quy vi file co the o thu muc con).
                src_lua = None
                for root, dirs, files in os.walk(extract_dir):
                    if lua_filename in files:
                        src_lua = os.path.join(root, lua_filename)
                        break
                if not src_lua or not os.path.exists(src_lua):
                    return {"success": False, "error": f"Không tìm thấy file {lua_filename} trong Lua.zip"}
                # Move DUY NHAT {appid}.lua -> Nexus folder.
                # Neu PermissionError (Steam o Program Files) -> spawn UAC copy.
                try:
                    if os.path.exists(target_lua):
                        os.remove(target_lua)
                    shutil.move(src_lua, target_lua)
                except PermissionError:
                    ok = self._elevated_copy_single(src_lua, nexus_dir)
                    if not ok:
                        return {"success": False,
                                "error": "Cần cấp quyền Administrator (UAC) để ghi vào thư mục Steam. Vui lòng bấm Yes khi hộp thoại UAC xuất hiện."}
            finally:
                self._cleanup_hideout(hidden_dir)
            return {"success": True, "already": False}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}

    def fix_game(self, steam_path, fix_data):
        """Fix game (online crack): gdown zip -> giai nen pass -> merge_and_replace vao game folder.
        Flow adapt tu Steam Project\\Fix.py (gdown + pyzipper + merge_and_replace).
        fix_data: {driveId, password, zipName, gameExe, gameFolder}.

        1. Check Steam path + game folder + game exe. Chua cai -> {success False, not_installed True}.
        2. Kill game exe neu dang chay.
        3. gdown zip -> giai nen pass -> merge_and_replace vao {steam}\\steamapps\\common\\{gameFolder}.
        4. Cleanup NexusHideout.
        """
        try:
            if not steam_path or not os.path.isdir(steam_path):
                return {"success": False, "error": "Thư mục Steam không tồn tại"}
            if not fix_data:
                return {"success": False, "error": "Thiếu dữ liệu fix"}

            drive_id = fix_data.get("driveId", "")
            password = fix_data.get("password", "")
            zip_name = fix_data.get("zipName", "Fix.zip")
            game_exe = fix_data.get("gameExe", "")
            game_folder = fix_data.get("gameFolder", "")

            if not (drive_id and password and game_exe and game_folder):
                return {"success": False, "error": "Dữ liệu fix không đầy đủ"}

            # 1. Check game folder + game exe (chua cai -> bao user).
            game_dir = os.path.join(steam_path, "steamapps", "common", game_folder)
            exe_path = os.path.join(game_dir, game_exe)
            if not os.path.isdir(game_dir) or not os.path.isfile(exe_path):
                return {"success": False, "not_installed": True,
                        "error": "Bạn chưa cài đặt game"}

            # 2. Kill game exe neu dang chay (tasklist check + taskkill).
            try:
                out = subprocess.check_output(
                    f'tasklist /FI "IMAGENAME eq {game_exe}" /NH',
                    shell=True, creationflags=134217728
                ).decode("utf-8", errors="ignore").lower()
                if game_exe.lower() in out:
                    subprocess.run(
                        f'taskkill /f /im {game_exe} >nul 2>&1',
                        shell=True, creationflags=134217728
                    )
                    time.sleep(1)
            except Exception:
                pass

            # 3. gdown zip -> giai nen -> merge_and_replace vao game folder.
            hidden_dir = self._prepare_hideout()
            try:
                zip_path = self._download_zip(hidden_dir, zip_name, drive_id)
                extract_dir = os.path.join(hidden_dir, "extracted_files")
                self._extract_zip(zip_path, extract_dir, password.encode("utf-8"))
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                # Merge TOAN BO file/folder vao game folder (replace).
                # Neu PermissionError (game folder o Program Files) -> spawn UAC robocopy.
                try:
                    self._merge_and_replace_all(extract_dir, game_dir)
                except PermissionError:
                    ok = self._elevated_merge(extract_dir, game_dir)
                    if not ok:
                        return {"success": False,
                                "error": "Cần cấp quyền Administrator (UAC) để ghi vào thư mục game. Vui lòng bấm Yes khi hộp thoại UAC xuất hiện."}
            finally:
                self._cleanup_hideout(hidden_dir)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}

    def redeem_code(self, code, expected_appid):
        """Redeem code TokeerDRM: validate -> check custom DLL -> POST server -> verify app_id -> ghi registry.
        Adapt tu TokeerDRM Open Source\\tokeer_drm.py (redeem method).

        1. Validate code dung 6 ky tu (letters/digits, case-insensitive). Sai -> {invalid True}.
        2. Check custom DLL Tesla697 da cai (marker .tokeer_ost_custom trong Steam root).
           Chua cai -> {not_installed True}.
        3. POST server /drm/redeem {code}. Server tra {app_id, appticket, eticket, uses_remaining}.
        4. Check app_id match expected_appid. Sai -> {wrong_game True}.
        5. Ghi AppTicket + ETicket vao HKCU\\Software\\Valve\\Steam\\Apps\\{appid}.
        """
        try:
            # 1. Validate 6 ky tu (chi quan tam length, khong quan tam loai ky tu).
            clean = (code or "").strip().upper()
            if len(clean) != 6:
                return {"success": False, "invalid": True}

            # 2. Check custom DLL Tesla697 da cai (marker file trong Steam root).
            steam_path = self._get_steam_install_path()
            if not steam_path:
                return {"success": False, "not_installed": True}
            marker = os.path.join(steam_path, ".tokeer_ost_custom")
            if not os.path.isfile(marker):
                return {"success": False, "not_installed": True}

            # 3. POST server /drm/redeem.
            try:
                import requests
            except ImportError:
                return {"success": False, "error": "Thiếu thư viện requests. Cài: pip install requests"}
            server_url = "http://31.57.38.79:8091"
            r = requests.post(server_url + "/drm/redeem", json={"code": clean}, timeout=25)
            data = r.json()
            if r.status_code != 200 or not data.get("success", False):
                reason = data.get("reason", data.get("error", "Server error"))
                return {"success": False, "code_not_found": True, "error": reason}

            app_id = data.get("app_id")
            appticket = data.get("appticket")
            eticket = data.get("eticket")
            uses_remaining = data.get("uses_remaining")
            if not (app_id and appticket and eticket):
                return {"success": False, "error": "Server trả về ticket không đầy đủ"}

            # 4. Check app_id match expected_appid (game hien tai).
            if str(app_id) != str(expected_appid):
                return {"success": False, "wrong_game": True, "returned_appid": str(app_id)}

            # 5. Ghi AppTicket + ETicket vao registry.
            try:
                import winreg
            except ImportError:
                return {"success": False, "error": "winreg không khả dụng (chỉ Windows)"}
            key_path = "Software\\Valve\\Steam\\Apps\\" + str(app_id)
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.SetValueEx(key, "AppTicket", 0, winreg.REG_BINARY, bytes.fromhex(appticket))
                winreg.SetValueEx(key, "ETicket", 0, winreg.REG_BINARY, bytes.fromhex(eticket))
            finally:
                winreg.CloseKey(key)
            return {"success": True, "app_id": str(app_id), "uses_remaining": uses_remaining}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {str(e)}"}


WEB_URL = 'https://steam-snowy.vercel.app/'
WIN_TITLE = 'NexusGames'

# Token bi mat: 32 ky tu hex. Webview inject vao page de unlock web (gate).
# Browser truy cap truc tiep khong co token -> lock screen.
NEXUS_TOKEN = secrets.token_hex(16)  # 32 ky tu


def set_dpi_aware():
    # Giup kich thuoc cua so dung tren man 4K / Windows scaling (125%, 150%...)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except: pass


def get_workarea():
    # Lay vung lam viec (khong tinh taskbar) -> cua so can chinh theo khu vuc nay
    rect = ctypes.wintypes.RECT()
    SPI_GETWORKAREA = 0x0030
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect.right - rect.left, rect.bottom - rect.top


def window_geometry():
    set_dpi_aware()
    try:
        w, h = get_workarea()
    except:
        w, h = 1920, 1080
    # Cua so = 87% workarea, can giua. min_size de khong nho qua.
    win_w = int(w * 0.87)
    win_h = int(h * 0.87)
    x = int((w - win_w) / 2)
    y = int((h - win_h) / 2)
    return win_w, win_h, x, y


def inject_token(window):
    # Inject token vao window cua page. Lap lai moi 150ms trong 3s de
    # chac chan duoc set ngay khi page load xong (race condition).
    js = f"window.__NEXUS_TOKEN = '{NEXUS_TOKEN}';"
    for _ in range(20):
        try:
            window.evaluate_js(js)
        except:
            pass
        import time
        time.sleep(0.15)


def on_loaded(window):
    # Event 'loaded' fire khi page load xong -> bat dau inject token
    threading.Thread(target=inject_token, args=(window,), daemon=True).start()


def is_admin():
    # Kiem tra process hien tai co quyen admin khong.
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    # Relaunch process hien tai voi quyen admin (UAC prompt 1 lan).
    # Tra True neu spawn thanh cong (user bam Yes), False neu user tu choi/loi.
    try:
        if getattr(sys, 'frozen', False):
            # Frozen exe (PyInstaller) — chay luon exe, khong can script arg.
            exe = sys.executable
            params = None
        else:
            # Script — chay python.exe voi duong dan script + args.
            # Dung abspath de moi working dir van resolve duoc (UAC co the chay tu System32).
            exe = sys.executable
            script = os.path.abspath(sys.argv[0])
            params = '"' + script + '"'
            if len(sys.argv) > 1:
                params += ' ' + ' '.join('"' + a + '"' for a in sys.argv[1:])
        # lpDirectory (tham so 5) = thu muc chua script -> working dir nhat quan.
        work_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if not getattr(sys, 'frozen', False) else None
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, work_dir, 1)
        return ret > 32
    except Exception:
        return False


def main():
    # Tu relaunch as admin ngay tu dau -> ghi Steam folder khong can UAC tung buoc.
    # Neu user tu choi UAC -> van chay binh thuong (per-op UAC fallback o install_nexust/share_game/fix_game).
    if not is_admin():
        if relaunch_as_admin():
            sys.exit(0)  # Da spawn process admin -> thoat process non-admin nay.
        # User tu choi UAC -> tiep tuc chay non-admin, fallback UAC tung op van hoat dong.
    import os
    win_w, win_h, x, y = window_geometry()
    window = webview.create_window(
        WIN_TITLE,
        WEB_URL,
        width=win_w,
        height=win_h,
        x=x,
        y=y,
        min_size=(1100, 680),
        resizable=True,
        background_color='#111317',
        js_api=NexusAPI(),
    )
    window.events.loaded += lambda: on_loaded(window)
    
    # Thiết lập thư mục lưu trữ cache, cookies, localstorage
    appdata = os.environ.get('APPDATA')
    if appdata:
        storage_dir = os.path.join(appdata, 'NexusGamesData')
    else:
        storage_dir = os.path.expanduser('~/NexusGamesData')
        
    try:
        os.makedirs(storage_dir, exist_ok=True)
    except:
        storage_dir = None
        
    webview.start(private_mode=False, storage_path=storage_dir)


if __name__ == "__main__":
    main()
