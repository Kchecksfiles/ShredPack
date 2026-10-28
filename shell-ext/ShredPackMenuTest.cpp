// Self-test for ShredPackMenu.dll, run by CI on Windows (and by build.bat). It loads the DLL the
// way COM does and checks, with real shell items:
//   * the menu title for several file names (spaces, Unicode, '&', very long, compound extensions)
//   * the generic fallback when there is no single selection
//   * Invoke() really starts "<dir>\ShredPack.exe --mode folder "<path>"" with the path intact
// It does NOT prove anything about Explorer itself - that needs a manual check on Windows.
//
// For the Invoke test this very program is copied next to the DLL as ShredPack.exe; when started
// with --mode it only records its command line to invoked.txt and exits.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shobjidl.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <stdio.h>
#include <io.h>
#include <fcntl.h>
#include <string>
#include <vector>

static const CLSID CLSID_ShredPackExtractTo =
    {0x6F3A1C52, 0x8B1E, 0x4D7A, {0x9C, 0x35, 0x2E, 0x8A, 0x5B, 0x7D, 0x41, 0xF0}};

static int g_failed = 0, g_checks = 0;
static void Check(bool ok, const std::wstring& what, const std::wstring& detail = L"")
{
    ++g_checks;
    if (!ok) ++g_failed;
    wprintf(L"%s %s%s%s\n", ok ? L"PASS" : L"FAIL", what.c_str(), detail.empty() ? L"" : L"  -> ", detail.c_str());
}

static std::wstring DirOfSelf()
{
    wchar_t buf[MAX_PATH * 2];
    GetModuleFileNameW(nullptr, buf, ARRAYSIZE(buf));
    std::wstring p = buf;
    return p.substr(0, p.find_last_of(L'\\'));
}

static IShellItemArray* ArrayFor(const std::wstring& path)
{
    IShellItem* item = nullptr;
    if (FAILED(SHCreateItemFromParsingName(path.c_str(), nullptr, IID_PPV_ARGS(&item)))) return nullptr;
    IShellItemArray* arr = nullptr;
    SHCreateShellItemArrayFromShellItem(item, IID_PPV_ARGS(&arr));
    item->Release();
    return arr;
}

static std::wstring TitleOf(IExplorerCommand* cmd, IShellItemArray* arr)
{
    LPWSTR t = nullptr;
    if (FAILED(cmd->GetTitle(arr, &t)) || !t) return L"<GetTitle failed>";
    std::wstring s = t;
    CoTaskMemFree(t);
    return s;
}

int wmain(int argc, wchar_t** argv)
{
    // ---- stand-in ShredPack.exe: record the command line and leave ----
    if (argc > 1 && wcscmp(argv[1], L"--mode") == 0) {
        std::wstring out = DirOfSelf() + L"\\invoked.txt";
        HANDLE f = CreateFileW(out.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (f != INVALID_HANDLE_VALUE) {
            std::wstring line = GetCommandLineW();
            DWORD n;
            WriteFile(f, line.c_str(), (DWORD)(line.size() * sizeof(wchar_t)), &n, nullptr);
            CloseHandle(f);
        }
        return 0;
    }

    _setmode(_fileno(stdout), _O_U8TEXT);        // print Unicode names without code-page errors
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    std::wstring dir = DirOfSelf();
    HMODULE dll = LoadLibraryW((dir + L"\\ShredPackMenu.dll").c_str());
    Check(dll != nullptr, L"DLL loads");
    if (!dll) return 1;
    typedef HRESULT(STDAPICALLTYPE * GetClassObjectFn)(REFCLSID, REFIID, void**);
    typedef HRESULT(STDAPICALLTYPE * CanUnloadFn)();
    GetClassObjectFn getClass = (GetClassObjectFn)GetProcAddress(dll, "DllGetClassObject");
    CanUnloadFn canUnload = (CanUnloadFn)GetProcAddress(dll, "DllCanUnloadNow");
    Check(getClass && canUnload, L"DllGetClassObject and DllCanUnloadNow are exported");
    if (!getClass) return 1;

    IClassFactory* factory = nullptr;
    Check(SUCCEEDED(getClass(CLSID_ShredPackExtractTo, IID_PPV_ARGS(&factory))) && factory, L"class factory for our CLSID");
    GUID other = {0x12345678, 0x1234, 0x1234, {1, 2, 3, 4, 5, 6, 7, 8}};
    void* nothing = nullptr;
    Check(getClass(other, IID_IClassFactory, &nothing) == CLASS_E_CLASSNOTAVAILABLE, L"a foreign CLSID is refused");
    if (!factory) return 1;
    IExplorerCommand* cmd = nullptr;
    Check(SUCCEEDED(factory->CreateInstance(nullptr, IID_PPV_ARGS(&cmd))) && cmd, L"IExplorerCommand instance");
    if (!cmd) return 1;

    // a scratch folder with files named like real archives
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    std::wstring work = std::wstring(tmp) + L"ShredPackMenuTest_" + std::to_wstring(GetCurrentProcessId());
    CreateDirectoryW(work.c_str(), nullptr);

    std::wstring longName = L"A very long archive file name that keeps going and going and going for ages 2024.zip";
    struct Case { std::wstring file; std::wstring expected; };
    std::vector<Case> cases = {
        {L"GTA V.zip", L"Extract to GTA V.zip"},
        {L"My Files.7z", L"Extract to My Files.7z"},
        {L"Backup.rar", L"Extract to Backup.rar"},
        {L"Game.tar.gz", L"Extract to Game.tar.gz"},
        {L"Z\u00fcrich \u65e5\u672c \U0001F600.zip", L"Extract to Z\u00fcrich \u65e5\u672c \U0001F600.zip"},
        {L"R&D Notes.zip", L"Extract to R&&D Notes.zip"},       // '&' is a menu accelerator: doubled
    };
    for (const Case& c : cases) {
        std::wstring path = work + L"\\" + c.file;
        HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
        IShellItemArray* arr = ArrayFor(path);
        std::wstring got = arr ? TitleOf(cmd, arr) : L"<no shell item>";
        Check(got == c.expected, L"title for \"" + c.file + L"\"", got == c.expected ? L"" : L"got \"" + got + L"\"");
        if (arr) arr->Release();
    }

    {   // a very long name is shortened but keeps its extension
        std::wstring path = work + L"\\" + longName;
        HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
        IShellItemArray* arr = ArrayFor(path);
        std::wstring got = arr ? TitleOf(cmd, arr) : L"";
        bool ok = got.size() < longName.size() && got.rfind(L"Extract to ", 0) == 0 && got.find(L'\u2026') != std::wstring::npos
                  && got.size() >= 4 && got.compare(got.size() - 4, 4, L".zip") == 0;
        Check(ok, L"a very long name is shortened with an ellipsis and keeps its extension", got);
        if (arr) arr->Release();
    }

    Check(TitleOf(cmd, nullptr) == L"Extract to <archive name>\\", L"no selection -> generic fallback title");
    EXPCMDSTATE state = ECS_HIDDEN;
    Check(SUCCEEDED(cmd->GetState(nullptr, FALSE, &state)) && state == ECS_ENABLED, L"GetState: enabled");
    EXPCMDFLAGS flags = (EXPCMDFLAGS)-1;
    Check(SUCCEEDED(cmd->GetFlags(&flags)) && flags == ECF_DEFAULT, L"GetFlags: default");

    // Invoke: copy this program next to the DLL as ShredPack.exe and see what it is started with
    {
        wchar_t self[MAX_PATH * 2];
        GetModuleFileNameW(nullptr, self, ARRAYSIZE(self));
        std::wstring fake = dir + L"\\ShredPack.exe";
        std::wstring invoked = dir + L"\\invoked.txt";
        DeleteFileW(invoked.c_str());
        bool copied = CopyFileW(self, fake.c_str(), FALSE) != 0;
        Check(copied, L"test setup: stand-in ShredPack.exe next to the DLL");
        std::wstring path = work + L"\\" + cases[4].file;          // spaces + Unicode + emoji
        IShellItemArray* arr = ArrayFor(path);
        HRESULT hr = arr ? cmd->Invoke(arr, nullptr) : E_FAIL;
        Check(SUCCEEDED(hr), L"Invoke succeeds");
        std::wstring recorded;
        for (int i = 0; i < 100 && recorded.empty(); ++i) {
            Sleep(100);
            HANDLE f = CreateFileW(invoked.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
            if (f != INVALID_HANDLE_VALUE) {
                wchar_t buf[8192];
                DWORD n = 0;
                if (ReadFile(f, buf, sizeof(buf) - 2, &n, nullptr) && n > 0) recorded.assign(buf, n / sizeof(wchar_t));
                CloseHandle(f);
            }
        }
        Check(recorded.find(L"--mode folder") != std::wstring::npos, L"Invoke starts ShredPack.exe with --mode folder", recorded);
        Check(recorded.find(L"\"" + path + L"\"") != std::wstring::npos, L"the archive path arrives intact (quoted, spaces and Unicode)", recorded);
        if (arr) arr->Release();
        DeleteFileW(fake.c_str());
        DeleteFileW(invoked.c_str());
    }

    cmd->Release();
    factory->Release();
    Check(canUnload && canUnload() == S_OK, L"the DLL can be unloaded once every object is released");
    FreeLibrary(dll);

    // clean up
    WIN32_FIND_DATAW fd;
    HANDLE find = FindFirstFileW((work + L"\\*").c_str(), &fd);
    if (find != INVALID_HANDLE_VALUE) {
        do { if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) DeleteFileW((work + L"\\" + fd.cFileName).c_str()); } while (FindNextFileW(find, &fd));
        FindClose(find);
    }
    RemoveDirectoryW(work.c_str());

    wprintf(L"\nMENU DLL RESULT: %d/%d checks passed\n", g_checks - g_failed, g_checks);
    return g_failed ? 1 : 0;
}
