// ShredPackMenu.dll - the one thing a registry-only Explorer menu cannot do: show the NAME of
// the archive that was right-clicked ("Extract to GTA V.zip").
//
// A minimal in-process COM server implementing IExplorerCommand. Explorer calls GetTitle()
// with the current selection every time the menu opens; Invoke() starts
//     "<folder of this DLL>\ShredPack.exe" --mode folder "<archive>" ...
// i.e. exactly what the static menu entry ran, so extraction behaviour is unchanged.
//
// No ATL/WRL, no C++ runtime state, no registry access: the DLL only reads the selection and
// starts a process. Registration (HKCU only) is done by ShredPack.exe --install.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shobjidl.h>
#include <shlwapi.h>
#include <new>
#include <string>
#include <vector>

// {6F3A1C52-8B1E-4D7A-9C35-2E8A5B7D41F0}  - must match MENU_CLSID in shredpack.py
static const CLSID CLSID_ShredPackExtractTo =
    {0x6F3A1C52, 0x8B1E, 0x4D7A, {0x9C, 0x35, 0x2E, 0x8A, 0x5B, 0x7D, 0x41, 0xF0}};

static HMODULE g_module = nullptr;
static volatile LONG g_objects = 0;     // live COM objects
static volatile LONG g_locks = 0;       // LockServer count

// SIGDN_PARENTRELATIVEFORPARSING: the file name with its extension (older MinGW headers lack the enum name)
static const SIGDN kNameWithExtension = (SIGDN)0x80018001;

static const wchar_t kFallbackTitle[] = L"Extract to <archive name>\\";
static const size_t kMaxName = 60;      // longest file name shown before it is shortened

// ---- helpers ---------------------------------------------------------------------------
// "Extract to <file name>". '&' is a menu accelerator, so it is doubled; a very long name keeps
// its beginning and its end (the extension) with an ellipsis between.
static std::wstring BuildTitle(const std::wstring& name)
{
    std::wstring shown = name;
    if (shown.size() > kMaxName) {
        std::wstring head = shown.substr(0, 36);
        std::wstring tail = shown.substr(shown.size() - 21);
        if (!head.empty() && head.back() >= 0xD800 && head.back() <= 0xDBFF) head.pop_back();   // don't split a surrogate pair
        if (!tail.empty() && tail.front() >= 0xDC00 && tail.front() <= 0xDFFF) tail.erase(0, 1);
        shown = head + L"\x2026" + tail;
    }
    std::wstring out = L"Extract to ";
    for (wchar_t c : shown) {
        if (c == L'&') out += L"&&";
        else out += c;
    }
    return out;
}

static std::wstring ModuleDir()
{
    wchar_t buf[4096];
    DWORD n = GetModuleFileNameW(g_module, buf, ARRAYSIZE(buf));
    if (n == 0 || n >= ARRAYSIZE(buf)) return L"";
    std::wstring path(buf, n);
    size_t slash = path.find_last_of(L'\\');
    return slash == std::wstring::npos ? L"" : path.substr(0, slash);
}

static std::wstring ExePath()
{
    std::wstring dir = ModuleDir();
    return dir.empty() ? L"" : dir + L"\\ShredPack.exe";
}

static HRESULT Launch(const std::wstring& exe, const std::vector<std::wstring>& files)
{
    std::wstring cmd = L"\"" + exe + L"\" --mode folder";
    for (const std::wstring& f : files) cmd += L" \"" + f + L"\"";
    std::vector<wchar_t> line(cmd.begin(), cmd.end());
    line.push_back(L'\0');
    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};
    if (!CreateProcessW(exe.c_str(), line.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi))
        return HRESULT_FROM_WIN32(GetLastError());
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return S_OK;
}

// ---- the command -----------------------------------------------------------------------
class ExtractToCommand final : public IExplorerCommand
{
public:
    ExtractToCommand() : m_refs(1) { InterlockedIncrement(&g_objects); }

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IExplorerCommand) {
            *ppv = static_cast<IExplorerCommand*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_refs); }
    IFACEMETHODIMP_(ULONG) Release() override
    {
        LONG n = InterlockedDecrement(&m_refs);
        if (n == 0) {
            InterlockedDecrement(&g_objects);
            delete this;
        }
        return n;
    }

    // IExplorerCommand
    IFACEMETHODIMP GetTitle(IShellItemArray* items, LPWSTR* name) override
    {
        if (!name) return E_POINTER;
        std::wstring title = kFallbackTitle;
        DWORD count = 0;
        if (items && SUCCEEDED(items->GetCount(&count)) && count == 1) {
            IShellItem* item = nullptr;
            if (SUCCEEDED(items->GetItemAt(0, &item)) && item) {
                PWSTR file = nullptr;
                // the real file name including its extension, whatever Explorer's
                // "hide extensions" setting is
                if (SUCCEEDED(item->GetDisplayName(kNameWithExtension, &file)) && file) {
                    title = BuildTitle(file);
                    CoTaskMemFree(file);
                }
                item->Release();
            }
        }
        return SHStrDupW(title.c_str(), name);
    }

    IFACEMETHODIMP GetIcon(IShellItemArray*, LPWSTR* icon) override
    {
        if (!icon) return E_POINTER;
        *icon = nullptr;
        std::wstring exe = ExePath();
        if (exe.empty()) return E_NOTIMPL;
        return SHStrDupW((L"\"" + exe + L"\",0").c_str(), icon);
    }

    IFACEMETHODIMP GetToolTip(IShellItemArray*, LPWSTR* tip) override
    {
        if (tip) *tip = nullptr;
        return E_NOTIMPL;
    }

    IFACEMETHODIMP GetCanonicalName(GUID* guid) override
    {
        if (guid) *guid = GUID_NULL;
        return E_NOTIMPL;
    }

    IFACEMETHODIMP GetState(IShellItemArray*, BOOL, EXPCMDSTATE* state) override
    {
        if (!state) return E_POINTER;
        *state = ECS_ENABLED;
        return S_OK;
    }

    IFACEMETHODIMP Invoke(IShellItemArray* items, IBindCtx*) override
    {
        if (!items) return E_INVALIDARG;
        DWORD count = 0;
        HRESULT hr = items->GetCount(&count);
        if (FAILED(hr)) return hr;
        std::wstring exe = ExePath();
        if (exe.empty()) return E_FAIL;

        std::vector<std::wstring> batch;
        size_t len = 0;
        HRESULT result = S_OK;
        for (DWORD i = 0; i < count; ++i) {
            IShellItem* item = nullptr;
            if (FAILED(items->GetItemAt(i, &item)) || !item) continue;
            PWSTR path = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path)) && path) {
                std::wstring p = path;
                CoTaskMemFree(path);
                if (len + p.size() + 4 > 30000 && !batch.empty()) {       // stay under the command-line limit
                    HRESULT h = Launch(exe, batch);
                    if (FAILED(h)) result = h;
                    batch.clear();
                    len = 0;
                }
                len += p.size() + 3;
                batch.push_back(p);
            }
            item->Release();
        }
        if (!batch.empty()) {
            HRESULT h = Launch(exe, batch);
            if (FAILED(h)) result = h;
        }
        return result;
    }

    IFACEMETHODIMP GetFlags(EXPCMDFLAGS* flags) override
    {
        if (!flags) return E_POINTER;
        *flags = ECF_DEFAULT;
        return S_OK;
    }

    IFACEMETHODIMP EnumSubCommands(IEnumExplorerCommand** e) override
    {
        if (e) *e = nullptr;
        return E_NOTIMPL;
    }

private:
    ~ExtractToCommand() {}
    volatile LONG m_refs;
};

// ---- class factory + DLL entry points ---------------------------------------------------
class Factory : public IClassFactory
{
public:
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) {
            *ppv = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return 2; }       // a single static instance
    IFACEMETHODIMP_(ULONG) Release() override { return 1; }
    IFACEMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override
    {
        if (!ppv) return E_POINTER;
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        ExtractToCommand* cmd = new (std::nothrow) ExtractToCommand();
        if (!cmd) return E_OUTOFMEMORY;
        HRESULT hr = cmd->QueryInterface(riid, ppv);
        cmd->Release();
        return hr;
    }
    IFACEMETHODIMP LockServer(BOOL lock) override
    {
        if (lock) InterlockedIncrement(&g_locks); else InterlockedDecrement(&g_locks);
        return S_OK;
    }
};
static Factory g_factory;

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (clsid != CLSID_ShredPackExtractTo) return CLASS_E_CLASSNOTAVAILABLE;
    return g_factory.QueryInterface(riid, ppv);
}

STDAPI DllCanUnloadNow()
{
    return (g_objects == 0 && g_locks == 0) ? S_OK : S_FALSE;
}
