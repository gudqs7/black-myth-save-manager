// Minimal in-process bridge for Phantom Blade Zero save injection.
//
// This DLL is injected by the save manager itself. It does not use UE4SS.
// It decodes PBZSaveGame.sav (PBZS + AES-256-GCM + Oodle), temporarily
// subclasses the game's main window, and posts one custom window message per
// slot. The window thread executes the UFunction call without touching any
// engine hook used by UE4SS.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <commctrl.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>


namespace {

constexpr int kRecordHeader = 36;
constexpr int kObjectItemSize = 0x18;
constexpr uint32_t kObjectsPerChunk = 64 * 1024;
constexpr uint32_t kRFClassDefaultObject = 0x10;

// PBZ Steam build 26367565. The bridge fails closed on any other executable.
constexpr uint32_t kBuildTimestamp = 1793568319;
constexpr uint32_t kBuildImageSize = 504082432;
constexpr uintptr_t kGObjectsRva = 0x0DE28C50;
constexpr uintptr_t kProcessEventRva = 0x01630720;
constexpr uintptr_t kFNameCtorRva = 0x01439BC0;

constexpr size_t kUObjectClassOffset = 0x10;
constexpr size_t kUObjectNameOffset = 0x18;
constexpr size_t kUObjectOuterOffset = 0x20;
constexpr size_t kUObjectFlagsOffset = 0x08;

constexpr size_t kSaveGameParamSlotOffset = 0x78;
constexpr size_t kSaveGameParamAdvancedOffset = 0x94;
constexpr size_t kSaveGameParamShowTipOffset = 0xA8;
constexpr size_t kSaveGameParamIoContextOffset = 0xA9;
constexpr size_t kSaveGameParamReasonOffset = 0xAA;
constexpr size_t kReplaceStringOffset = 0xC0;
constexpr size_t kReplaceReturnOffset = 0xD0;
constexpr size_t kParamBufferSize = 0x200;
constexpr UINT kBridgeStepMessage = WM_APP + 0x5A1;
constexpr UINT_PTR kBridgeSubclassId = 0x50425A31;

const unsigned char kKeyA[32] = {
    0xec, 0x88, 0xa5, 0x76, 0x0a, 0xcd, 0xfb, 0x3b, 0x30, 0xd9, 0xf0, 0xb8, 0xad, 0x07, 0xc5, 0x2a,
    0xc6, 0xda, 0x79, 0x18, 0x37, 0x61, 0x23, 0x4a, 0xf9, 0x1e, 0x8b, 0x71, 0x08, 0x28, 0x7c, 0x7b};
const unsigned char kKeyB[32] = {
    0xf9, 0x29, 0x5b, 0x79, 0x5f, 0x2d, 0x10, 0x12, 0xbe, 0xa5, 0x64, 0xd9, 0xf9, 0x84, 0x94, 0x43,
    0xac, 0xf0, 0x7e, 0x18, 0xda, 0x4c, 0x34, 0xf0, 0xe6, 0x99, 0x9f, 0x26, 0xe0, 0xd1, 0x42, 0x56};

struct FName {
    uint32_t index = 0;
    uint32_t number = 0;
};

struct FStringView {
    wchar_t* data = nullptr;
    int32_t num = 0;
    int32_t max = 0;
};

struct FUObjectItem {
    uint32_t flags_and_ref_count;
    uint32_t remote_id;
    void* object;
    int32_t serial_number;
    int32_t cluster_root_index;
};
static_assert(sizeof(FUObjectItem) == kObjectItemSize, "Unexpected FUObjectItem size");

struct SlotData {
    std::string name;
    std::vector<unsigned char> json;
};

struct JobConfig {
    std::wstring save_path;
    std::wstring oodle_path;
    std::wstring result_path;
    std::wstring log_path;
    int delay_ms = 50;
};

struct InjectionState {
    std::vector<SlotData> slots;
    size_t next_index = 0;
    int injected = 0;
    int failed = 0;
    int last_result = -1;
    bool targets_ready = false;
    void* subsystem = nullptr;
    void* replace_fn = nullptr;
    std::wstring error;
};

using ProcessEventFn = void(__fastcall*)(void*, void*, void*);
using FNameCtorFn = void(__fastcall*)(FName*, const wchar_t*, int);
using OodleDecompressFn = int64_t (*)(const void*, int64_t, void*, int64_t, int, int, int, void*, int64_t,
                                     void*, void*, void*, int64_t, int);

ProcessEventFn g_process_event_target = nullptr;
FNameCtorFn g_fname_ctor = nullptr;
uintptr_t g_game_base = 0;
uintptr_t g_gobjects = 0;

std::atomic<bool> g_job_active{false};
std::mutex g_run_mutex;
InjectionState g_state;
std::wstring g_current_log_path;
WNDPROC g_original_wndproc = nullptr;
bool g_using_window_long_ptr = false;


std::wstring utf8_to_wide(const std::string& value) {
    if (value.empty()) return {};
    int count = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (count <= 0) return {};
    std::wstring out(static_cast<size_t>(count), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), count);
    return out;
}

std::string wide_to_utf8(const std::wstring& value) {
    if (value.empty()) return {};
    int count = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0,
                                    nullptr, nullptr);
    if (count <= 0) return {};
    std::string out(static_cast<size_t>(count), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), count, nullptr,
                        nullptr);
    return out;
}


bool read_file(const std::wstring& path, std::vector<unsigned char>& out) {
    HANDLE handle = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(handle, &size) || size.QuadPart <= 0 || size.QuadPart > 0x7FFFFFFF) {
        CloseHandle(handle);
        return false;
    }
    out.resize(static_cast<size_t>(size.QuadPart));
    DWORD total = 0;
    while (total < out.size()) {
        DWORD got = 0;
        DWORD request = static_cast<DWORD>(std::min<size_t>(out.size() - total, 0x7FFFFFFF));
        if (!ReadFile(handle, out.data() + total, request, &got, nullptr) || got == 0) {
            CloseHandle(handle);
            return false;
        }
        total += got;
    }
    CloseHandle(handle);
    return true;
}

bool write_file(const std::wstring& path, const void* data, size_t size) {
    HANDLE handle = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS,
                                FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return false;
    DWORD total = 0;
    const auto* bytes = static_cast<const unsigned char*>(data);
    while (total < size) {
        DWORD wrote = 0;
        DWORD request = static_cast<DWORD>(std::min<size_t>(size - total, 0x7FFFFFFF));
        if (!WriteFile(handle, bytes + total, request, &wrote, nullptr) || wrote == 0) {
            CloseHandle(handle);
            return false;
        }
        total += wrote;
    }
    CloseHandle(handle);
    return true;
}

void append_log(const std::wstring& path, const std::string& text) {
    if (path.empty()) return;
    HANDLE handle = CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return;
    DWORD wrote = 0;
    WriteFile(handle, text.data(), static_cast<DWORD>(text.size()), &wrote, nullptr);
    CloseHandle(handle);
}

std::string sanitize_error(std::string text) {
    std::replace(text.begin(), text.end(), '\r', ' ');
    std::replace(text.begin(), text.end(), '\n', ' ');
    return text;
}

void write_result(const std::wstring& path, bool ok, const std::wstring& error, size_t slots, int injected,
                  int failed) {
    if (path.empty()) return;
    std::string text = std::string(ok ? "ok" : "fail") + " slots=" + std::to_string(slots) +
                       " injected=" + std::to_string(injected) + " failed=" + std::to_string(failed) +
                       " error=" + sanitize_error(wide_to_utf8(error)) + "\r\n";
    write_file(path, text.data(), text.size());
}

bool aes_gcm_decrypt(const unsigned char* key, const unsigned char* nonce, size_t nonce_len,
                     const unsigned char* tag, size_t tag_len, const unsigned char* ciphertext,
                     size_t ciphertext_len, std::vector<unsigned char>& out) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_KEY_HANDLE key_handle = nullptr;
    bool ok = false;
    do {
        if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_AES_ALGORITHM, nullptr, 0))) break;
        if (!BCRYPT_SUCCESS(BCryptSetProperty(
                alg, BCRYPT_CHAINING_MODE,
                reinterpret_cast<PUCHAR>(const_cast<wchar_t*>(BCRYPT_CHAIN_MODE_GCM)),
                sizeof(BCRYPT_CHAIN_MODE_GCM), 0))) {
            break;
        }
        if (!BCRYPT_SUCCESS(BCryptGenerateSymmetricKey(alg, &key_handle, nullptr, 0,
                                                       const_cast<PUCHAR>(key), 32, 0))) {
            break;
        }
        BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO info;
        BCRYPT_INIT_AUTH_MODE_INFO(info);
        info.pbNonce = const_cast<PUCHAR>(nonce);
        info.cbNonce = static_cast<ULONG>(nonce_len);
        info.pbTag = const_cast<PUCHAR>(tag);
        info.cbTag = static_cast<ULONG>(tag_len);

        out.resize(ciphertext_len);
        ULONG written = 0;
        NTSTATUS status = BCryptDecrypt(key_handle, const_cast<PUCHAR>(ciphertext),
                                        static_cast<ULONG>(ciphertext_len), &info, nullptr, 0,
                                        ciphertext_len ? out.data() : nullptr,
                                        static_cast<ULONG>(out.size()), &written, 0);
        if (!BCRYPT_SUCCESS(status)) break;
        out.resize(written);
        ok = true;
    } while (false);

    if (key_handle) BCryptDestroyKey(key_handle);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    return ok;
}

bool decrypt_record(const unsigned char* blob, size_t length, std::vector<unsigned char>& out) {
    if (length < static_cast<size_t>(kRecordHeader)) return false;
    if (memcmp(blob, "PBZS", 4) != 0 || blob[4] != 1 || blob[5] != 1) return false;
    unsigned char key[32];
    for (int i = 0; i < 32; ++i) key[i] = kKeyA[i] ^ kKeyB[i];
    return aes_gcm_decrypt(key, blob + 8, 12, blob + 20, 16, blob + kRecordHeader, length - kRecordHeader,
                           out);
}

struct Entry {
    std::string name;
    uint32_t uncompressed = 0;
    uint32_t offset = 0;
    uint32_t encrypted = 0;
};

bool parse_index(const std::vector<unsigned char>& plain, std::vector<Entry>& entries) {
    if (plain.size() < 12 || memcmp(plain.data(), "PBZS", 4) != 0) return false;
    size_t pos = 12;
    while (pos < plain.size()) {
        if (pos + 2 > plain.size()) return false;
        uint16_t name_len = static_cast<uint16_t>(plain[pos] | (plain[pos + 1] << 8));
        pos += 2;
        if (name_len == 0 || pos + name_len + 14 > plain.size()) return false;
        Entry entry;
        entry.name.assign(reinterpret_cast<const char*>(plain.data() + pos), name_len);
        pos += name_len + 2;
        memcpy(&entry.uncompressed, plain.data() + pos, 4);
        memcpy(&entry.offset, plain.data() + pos + 4, 4);
        memcpy(&entry.encrypted, plain.data() + pos + 8, 4);
        pos += 12;
        entries.push_back(std::move(entry));
    }
    return !entries.empty();
}

HMODULE load_oodle_module(const std::wstring& path) {
    if (path.empty()) return nullptr;
    return LoadLibraryW(path.c_str());
}

bool decode_all_slots(const std::wstring& save_path, const std::wstring& oodle_path,
                      std::vector<SlotData>& slots, std::wstring& error) {
    std::vector<unsigned char> data;
    if (!read_file(save_path, data)) {
        error = L"无法读取 PBZSaveGame.sav";
        return false;
    }
    if (data.size() < 12 || memcmp(data.data(), "PBZS", 4) != 0) {
        error = L"不是有效的 PBZS 存档容器";
        return false;
    }
    uint32_t index_size = 0;
    memcpy(&index_size, data.data() + 8, 4);
    if (index_size == 0 || 12ull + index_size > data.size()) {
        error = L"PBZS 索引大小无效";
        return false;
    }
    std::vector<unsigned char> index_plain;
    if (!decrypt_record(data.data() + 12, index_size, index_plain)) {
        error = L"PBZS 索引解密失败";
        return false;
    }
    std::vector<Entry> entries;
    if (!parse_index(index_plain, entries)) {
        error = L"PBZS 索引解析失败";
        return false;
    }

    HMODULE oodle_module = load_oodle_module(oodle_path);
    if (!oodle_module) {
        error = L"无法加载 oo2core_9_win64.dll";
        return false;
    }
    auto oodle = reinterpret_cast<OodleDecompressFn>(GetProcAddress(oodle_module, "OodleLZ_Decompress"));
    if (!oodle) {
        error = L"oo2core 中缺少 OodleLZ_Decompress";
        return false;
    }

    const size_t base = 12ull + index_size;
    std::vector<unsigned char> compressed;
    std::vector<SlotData> decoded;
    decoded.reserve(entries.size());
    for (const Entry& entry : entries) {
        if (base + entry.offset + entry.encrypted > data.size() || entry.uncompressed == 0 ||
            entry.uncompressed > 0x10000000) {
            error = L"PBZS 数据块范围无效";
            return false;
        }
        if (!decrypt_record(data.data() + base + entry.offset, entry.encrypted, compressed)) {
            error = L"PBZS 数据块解密失败: " + utf8_to_wide(entry.name);
            return false;
        }
        std::vector<unsigned char> destination(static_cast<size_t>(entry.uncompressed) + 65536, 0);
        std::vector<unsigned char> source(compressed.size() + 65536, 0);
        memcpy(source.data(), compressed.data(), compressed.size());
        int64_t got = oodle(source.data(), static_cast<int64_t>(compressed.size()), destination.data(),
                            static_cast<int64_t>(entry.uncompressed), 1, 0, 0, nullptr, 0, nullptr,
                            nullptr, nullptr, 0, 3);
        if (got != static_cast<int64_t>(entry.uncompressed)) {
            error = L"Oodle 解压失败: " + utf8_to_wide(entry.name);
            return false;
        }
        SlotData slot;
        slot.name = entry.name;
        slot.json.assign(destination.begin(), destination.begin() + entry.uncompressed);
        decoded.push_back(std::move(slot));
    }

    slots.clear();
    slots.reserve(decoded.size());
    for (auto& slot : decoded) {
        if (slot.name != "__Advanced__") slots.push_back(std::move(slot));
    }
    for (auto& slot : decoded) {
        if (slot.name == "__Advanced__") slots.push_back(std::move(slot));
    }
    return !slots.empty();
}

bool parse_job_file(const std::wstring& path, JobConfig& config, std::wstring& error) {
    std::vector<unsigned char> bytes;
    if (!read_file(path, bytes)) {
        error = L"无法读取注入任务文件";
        return false;
    }
    std::string text(reinterpret_cast<const char*>(bytes.data()), bytes.size());
    size_t start = 0;
    while (start <= text.size()) {
        size_t end = text.find('\n', start);
        if (end == std::string::npos) end = text.size();
        std::string line = text.substr(start, end - start);
        start = end + 1;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t equals = line.find('=');
        if (equals == std::string::npos) continue;
        std::string key = line.substr(0, equals);
        std::string value = line.substr(equals + 1);
        if (key == "save") config.save_path = utf8_to_wide(value);
        else if (key == "oodle") config.oodle_path = utf8_to_wide(value);
        else if (key == "result") config.result_path = utf8_to_wide(value);
        else if (key == "log") config.log_path = utf8_to_wide(value);
        else if (key == "delay_ms") config.delay_ms = std::max(0, atoi(value.c_str()));
        if (end == text.size()) break;
    }
    if (config.save_path.empty() || config.oodle_path.empty() || config.result_path.empty()) {
        error = L"注入任务缺少 save/oodle/result 路径";
        return false;
    }
    config.delay_ms = std::min(config.delay_ms, 1000);
    return true;
}

bool validate_game_build(std::wstring& error) {
    HMODULE game = GetModuleHandleW(nullptr);
    if (!game) {
        error = L"无法获取 PBZ.exe 模块";
        return false;
    }
    auto* base = reinterpret_cast<unsigned char*>(game);
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) {
        error = L"PBZ.exe PE 头无效";
        return false;
    }
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
        error = L"PBZ.exe 不是 x64 PE";
        return false;
    }
    if (nt->FileHeader.TimeDateStamp != kBuildTimestamp || nt->OptionalHeader.SizeOfImage != kBuildImageSize) {
        char message[256];
        snprintf(message, sizeof(message),
                 "当前 PBZ.exe 版本不匹配（timestamp=%lu image=%lu），桥接仅支持 Steam build 26367565",
                 static_cast<unsigned long>(nt->FileHeader.TimeDateStamp),
                 static_cast<unsigned long>(nt->OptionalHeader.SizeOfImage));
        error = utf8_to_wide(message);
        return false;
    }
    uintptr_t process_event = reinterpret_cast<uintptr_t>(base + kProcessEventRva);
    uintptr_t fname_ctor = reinterpret_cast<uintptr_t>(base + kFNameCtorRva);
    g_game_base = reinterpret_cast<uintptr_t>(game);
    g_gobjects = g_game_base + kGObjectsRva;
    g_process_event_target = reinterpret_cast<ProcessEventFn>(process_event);
    g_fname_ctor = reinterpret_cast<FNameCtorFn>(fname_ctor);
    return true;
}

bool validate_object_array(std::wstring& error) {
    if (!g_gobjects) {
        error = L"GObjects 未初始化";
        return false;
    }
    auto* objects = reinterpret_cast<unsigned char*>(g_gobjects);
    auto num_elements = *reinterpret_cast<int32_t*>(objects + 0x20);
    auto num_chunks = *reinterpret_cast<int32_t*>(objects + 0x14);
    auto chunks = *reinterpret_cast<void***>(objects + 0x18);
    if (num_elements < 1000 || num_elements > 10000000 || num_chunks <= 0 || num_chunks > 1000 || !chunks) {
        error = L"GObjects 地址校验失败";
        return false;
    }
    if (!chunks[0]) {
        error = L"GObjects 首个对象块无效";
        return false;
    }
    return true;
}

FName make_fname(const wchar_t* text) {
    FName result{};
    if (g_fname_ctor) g_fname_ctor(&result, text, 0);
    return result;
}

bool fname_equals(const FName& left, const FName& right) {
    return left.index == right.index && left.number == right.number;
}

FName object_name(void* object) {
    if (!object) return {};
    return *reinterpret_cast<FName*>(static_cast<unsigned char*>(object) + kUObjectNameOffset);
}

void* object_class(void* object) {
    if (!object) return nullptr;
    return *reinterpret_cast<void**>(static_cast<unsigned char*>(object) + kUObjectClassOffset);
}

void* object_outer(void* object) {
    if (!object) return nullptr;
    return *reinterpret_cast<void**>(static_cast<unsigned char*>(object) + kUObjectOuterOffset);
}

int32_t object_flags(void* object) {
    if (!object) return 0;
    return *reinterpret_cast<int32_t*>(static_cast<unsigned char*>(object) + kUObjectFlagsOffset);
}

template <typename Fn>
bool for_each_uobject(Fn&& fn) {
    auto* gobjects = reinterpret_cast<unsigned char*>(g_gobjects);
    int32_t num_elements = *reinterpret_cast<int32_t*>(gobjects + 0x20);
    auto chunks = *reinterpret_cast<FUObjectItem***>(gobjects + 0x18);
    if (!chunks || num_elements <= 0) return false;
    for (int32_t index = 0; index < num_elements; ++index) {
        FUObjectItem* chunk = chunks[index / kObjectsPerChunk];
        if (!chunk) continue;
        FUObjectItem& item = chunk[index % kObjectsPerChunk];
        if (!item.object) continue;
        if (fn(item.object)) return true;
    }
    return false;
}

bool find_targets(std::wstring& error) {
    append_log(g_current_log_path, "bridge: constructing target FNames\r\n");
    FName subsystem_name = make_fname(L"PBZSaveLoadSubsystem");
    FName replace_name = make_fname(L"ReplaceSaveString");
    FName game_instance_name = make_fname(L"BP_GameInstance_C");
    FName default_object_name = make_fname(L"Default__PBZSaveLoadSubsystem");
    FName class_name = make_fname(L"Class");
    FName core_object_name = make_fname(L"Object");
    append_log(g_current_log_path,
               "bridge: target FNames ready subsystem=" + std::to_string(subsystem_name.index) +
                   " replace=" + std::to_string(replace_name.index) + " class=" + std::to_string(class_name.index) +
                   "\r\n");
    if (subsystem_name.index == 0 || replace_name.index == 0) {
        error = L"FName 构造失败，无法定位存档子系统";
        return false;
    }

    auto* gobjects = reinterpret_cast<unsigned char*>(g_gobjects);
    int32_t object_count = *reinterpret_cast<int32_t*>(gobjects + 0x20);
    int32_t chunk_count = *reinterpret_cast<int32_t*>(gobjects + 0x14);
    append_log(g_current_log_path, "bridge: objects=" + std::to_string(object_count) +
               " chunks=" + std::to_string(chunk_count) + "\r\n");

    void* subsystem = nullptr;
    void* fallback = nullptr;
    int valid_objects = 0;
    int class_name_matches = 0;
    int core_object_matches = 0;
    int subsystem_class_matches = 0;
    for_each_uobject([&](void* object) {
        void* cls = object_class(object);
        if (!cls) return false;
        ++valid_objects;
        FName cls_name = object_name(cls);
        if (fname_equals(cls_name, class_name)) ++class_name_matches;
        if (fname_equals(object_name(object), core_object_name)) ++core_object_matches;
        if (!fname_equals(cls_name, subsystem_name)) return false;
        ++subsystem_class_matches;
        if ((object_flags(object) & kRFClassDefaultObject) != 0) return false;
        if (fname_equals(object_name(object), default_object_name)) return false;
        void* outer = object_outer(object);
        void* outer_class = object_class(outer);
        if (outer_class && fname_equals(object_name(outer_class), game_instance_name)) {
            subsystem = object;
            return true;
        }
        if (!fallback) fallback = object;
        return false;
    });
    append_log(g_current_log_path,
               "bridge: subsystem scan complete objects=" + std::to_string(valid_objects) +
                   " classMatches=" + std::to_string(class_name_matches) +
                   " objectMatches=" + std::to_string(core_object_matches) +
                   " subsystemMatches=" + std::to_string(subsystem_class_matches) + "\r\n");
    if (!subsystem) subsystem = fallback;
    if (!subsystem) {
        error = L"未找到 PBZSaveLoadSubsystem 实例，请先进入游戏标题页";
        return false;
    }

    void* subsystem_class = object_class(subsystem);
    void* replace_fn = nullptr;
    for_each_uobject([&](void* object) {
        if (!fname_equals(object_name(object), replace_name)) return false;
        if (object_outer(object) != subsystem_class) return false;
        replace_fn = object;
        return true;
    });
    append_log(g_current_log_path, "bridge: function scan complete\r\n");
    if (!replace_fn) {
        error = L"未找到 PBZSaveLoadSubsystem.ReplaceSaveString";
        return false;
    }
    append_log(g_current_log_path, "bridge: found subsystem and ReplaceSaveString\r\n");

    g_state.subsystem = subsystem;
    g_state.replace_fn = replace_fn;
    g_state.targets_ready = true;
    uint16_t parms_size = *reinterpret_cast<uint16_t*>(static_cast<unsigned char*>(replace_fn) + 0x106);
    append_log(g_current_log_path, "bridge: targets ready parms_size=" + std::to_string(parms_size) + "\r\n");
    return true;
}

bool inject_slot(const SlotData& slot) {
    std::wstring slot_name = utf8_to_wide(slot.name);
    if (slot_name.empty()) return false;
    std::wstring json_wide = utf8_to_wide(std::string(
        reinterpret_cast<const char*>(slot.json.data()), slot.json.size()));
    if (json_wide.empty()) return false;

    alignas(16) unsigned char params[kParamBufferSize] = {};
    auto* slot_string = reinterpret_cast<FStringView*>(params + kSaveGameParamSlotOffset);
    slot_string->data = slot_name.data();
    slot_string->num = static_cast<int32_t>(slot_name.size() + 1);
    slot_string->max = slot_string->num;
    params[kSaveGameParamAdvancedOffset] = 0;
    params[kSaveGameParamShowTipOffset] = 0;
    params[kSaveGameParamIoContextOffset] = 0;
    params[kSaveGameParamReasonOffset] = 0;

    auto* replace_string = reinterpret_cast<FStringView*>(params + kReplaceStringOffset);
    replace_string->data = json_wide.data();
    replace_string->num = static_cast<int32_t>(json_wide.size() + 1);
    replace_string->max = replace_string->num;

    append_log(g_current_log_path, "bridge: calling ProcessEvent\r\n");
    g_process_event_target(g_state.subsystem, g_state.replace_fn, params);
    append_log(g_current_log_path, "bridge: ProcessEvent returned\r\n");
    g_state.last_result = static_cast<int>(params[kReplaceReturnOffset]);
    // PBZSaveReload treats a completed ProcessEvent call as success; the reflected
    // FSaveGameResultParam is diagnostic and can be Failed for slots whose async
    // disk-side status is irrelevant to the in-memory replacement.
    return true;
}

void run_next_slot() {
    if (!g_job_active.load(std::memory_order_acquire) || !g_state.error.empty()) return;
    if (!g_state.targets_ready) {
        append_log(g_current_log_path, "bridge: finding targets\r\n");
        std::wstring error;
        if (!find_targets(error)) {
            g_state.error = error;
            return;
        }
    }
    if (g_state.next_index >= g_state.slots.size()) return;

    const SlotData& slot = g_state.slots[g_state.next_index];
    append_log(g_current_log_path, "bridge: injecting slot " + slot.name + "\r\n");
    bool ok = inject_slot(slot);
    if (ok) {
        ++g_state.injected;
    } else {
        ++g_state.failed;
        g_state.error = L"槽位数据结构构造失败: " + utf8_to_wide(slot.name);
    }
    append_log(g_current_log_path,
               std::string(ok ? "slot ok: " : "slot fail: ") + slot.name +
                   " reflected=" + std::to_string(g_state.last_result) + "\r\n");
    ++g_state.next_index;
}

struct WindowSearch {
    HWND window = nullptr;
    LONG64 area = -1;
};

BOOL CALLBACK find_main_window_callback(HWND window, LPARAM parameter) {
    DWORD pid = 0;
    GetWindowThreadProcessId(window, &pid);
    if (pid != GetCurrentProcessId() || !IsWindowVisible(window) || GetWindowTextLengthW(window) == 0) {
        return TRUE;
    }
    RECT rect{};
    if (!GetClientRect(window, &rect)) return TRUE;
    LONG64 area = static_cast<LONG64>(rect.right - rect.left) * static_cast<LONG64>(rect.bottom - rect.top);
    auto* search = reinterpret_cast<WindowSearch*>(parameter);
    if (area > search->area) {
        search->area = area;
        search->window = window;
    }
    return TRUE;
}

HWND find_main_window() {
    WindowSearch search;
    EnumWindows(find_main_window_callback, reinterpret_cast<LPARAM>(&search));
    return search.window;
}

LRESULT CALLBACK bridge_subclass_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam,
                                      UINT_PTR subclass_id, DWORD_PTR ref_data) {
    (void)subclass_id;
    (void)ref_data;
    if (message == kBridgeStepMessage) {
        run_next_slot();
        return 0;
    }
    return DefSubclassProc(window, message, wparam, lparam);
}

LRESULT CALLBACK bridge_wndproc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == kBridgeStepMessage) {
        run_next_slot();
        return 0;
    }
    return CallWindowProcW(g_original_wndproc, window, message, wparam, lparam);
}

bool install_window_dispatch(HWND window, std::wstring& error) {
    if (SetWindowSubclass(window, bridge_subclass_proc, kBridgeSubclassId, 0)) {
        append_log(g_current_log_path, "bridge: window subclass installed\r\n");
        return true;
    }
    DWORD subclass_error = GetLastError();
    SetLastError(0);
    LONG_PTR original = SetWindowLongPtrW(window, GWLP_WNDPROC,
                                          reinterpret_cast<LONG_PTR>(bridge_wndproc));
    DWORD wndproc_error = GetLastError();
    if (original == 0 && wndproc_error != 0) {
        error = L"无法安装主窗口消息调度（subclass=" + std::to_wstring(subclass_error) +
                L", wndproc=" + std::to_wstring(wndproc_error) + L"）";
        return false;
    }
    g_original_wndproc = reinterpret_cast<WNDPROC>(original);
    g_using_window_long_ptr = true;
    append_log(g_current_log_path, "bridge: window procedure installed via SetWindowLongPtr\r\n");
    return true;
}

void remove_window_dispatch(HWND window) {
    if (g_using_window_long_ptr) {
        SetWindowLongPtrW(window, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(g_original_wndproc));
        g_original_wndproc = nullptr;
        g_using_window_long_ptr = false;
    } else {
        RemoveWindowSubclass(window, bridge_subclass_proc, kBridgeSubclassId);
    }
    append_log(g_current_log_path, "bridge: window dispatch removed\r\n");
}

bool run_job(const std::wstring& job_path) {
    JobConfig config;
    std::wstring error;
    if (!parse_job_file(job_path, config, error)) {
        if (!config.result_path.empty()) write_result(config.result_path, false, error, 0, 0, 0);
        return false;
    }
    g_current_log_path = config.log_path;
    append_log(g_current_log_path, "bridge: start save=" + wide_to_utf8(config.save_path) + "\r\n");

    if (!validate_game_build(error)) {
        write_result(config.result_path, false, error, 0, 0, 0);
        append_log(g_current_log_path, "bridge: " + sanitize_error(wide_to_utf8(error)) + "\r\n");
        return false;
    }
    if (!validate_object_array(error)) {
        write_result(config.result_path, false, error, 0, 0, 0);
        append_log(g_current_log_path, "bridge: " + sanitize_error(wide_to_utf8(error)) + "\r\n");
        return false;
    }

    g_state = {};
    if (!decode_all_slots(config.save_path, config.oodle_path, g_state.slots, error)) {
        write_result(config.result_path, false, error, 0, 0, 0);
        append_log(g_current_log_path, "bridge: " + sanitize_error(wide_to_utf8(error)) + "\r\n");
        return false;
    }
    append_log(g_current_log_path, "bridge: decoded slots=" + std::to_string(g_state.slots.size()) + "\r\n");

    HWND window = find_main_window();
    if (!window) {
        error = L"未找到 PBZ 主窗口";
        write_result(config.result_path, false, error, g_state.slots.size(), 0, 0);
        append_log(g_current_log_path, "bridge: " + sanitize_error(wide_to_utf8(error)) + "\r\n");
        return false;
    }
    if (!install_window_dispatch(window, error)) {
        write_result(config.result_path, false, error, g_state.slots.size(), 0, 0);
        append_log(g_current_log_path, "bridge: " + sanitize_error(wide_to_utf8(error)) + "\r\n");
        return false;
    }

    g_job_active.store(true, std::memory_order_release);
    bool dispatched = true;
    for (size_t index = 0; index < g_state.slots.size(); ++index) {
        DWORD_PTR message_result = 0;
        LRESULT sent = SendMessageTimeoutW(window, kBridgeStepMessage, 0, 0,
                                           SMTO_BLOCK | SMTO_ABORTIFHUNG, 5000, &message_result);
        if (sent == 0) {
            error = L"向游戏窗口投递内存写入任务超时";
            dispatched = false;
            break;
        }
        if (!g_state.error.empty()) {
            error = g_state.error;
            dispatched = false;
            break;
        }
        if (config.delay_ms > 0) Sleep(static_cast<DWORD>(config.delay_ms));
    }
    g_job_active.store(false, std::memory_order_release);
    remove_window_dispatch(window);

    bool ok = dispatched && error.empty() && g_state.failed == 0 &&
              g_state.next_index == g_state.slots.size();
    if (!ok && error.empty()) error = L"部分槽位未完成写入";
    write_result(config.result_path, ok, error, g_state.slots.size(), g_state.injected, g_state.failed);
    append_log(g_current_log_path,
               std::string(ok ? "bridge: ok" : "bridge: fail") + " slots=" +
                   std::to_string(g_state.slots.size()) + " injected=" + std::to_string(g_state.injected) +
                   " failed=" + std::to_string(g_state.failed) + " error=" + sanitize_error(wide_to_utf8(error)) +
                   "\r\n");
    return ok;
}

}  // namespace

extern "C" __declspec(dllexport) DWORD WINAPI PBZ_RunJobW(LPCWSTR job_path) {
    std::wstring path = job_path ? std::wstring(job_path) : std::wstring();
    std::lock_guard<std::mutex> lock(g_run_mutex);
    if (path.empty()) return 2;
    return run_job(path) ? 0 : 1;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}
