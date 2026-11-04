#pragma once

#include <windows.h>
#include <atomic>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

namespace tests {
namespace fs = std::filesystem;

// 断言失败时抛出包含源文件、行号和表达式的 runtime_error，由测试入口统计失败。
inline void Check(bool condition, const char* expression, const char* file, int line) {
    if (!condition) {
        throw std::runtime_error(std::string(file) + ":" + std::to_string(line) + ": " + expression);
    }
}

// 保留调用位置与原始表达式文本，条件表达式只求值一次。
#define CHECK(...) ::tests::Check((__VA_ARGS__), #__VA_ARGS__, __FILE__, __LINE__)

// 在当前线程执行一次动作，要求抛出 std::exception 派生异常；其他异常不在此捕获。
template <class Function> void CheckThrows(Function&& action) {
    bool caught = false;
    try {
        action();
    } catch (const std::exception&) {
        caught = true;
    }
    CHECK(caught);
}

// 测试名称用于过滤与输出；run 由测试入口顺序调用，失败通过异常报告。
struct TestCase {
    std::string name;
    std::function<void()> run;
};
// 按注册顺序拥有测试名称和回调，注册函数只追加用例而不执行。
using TestList = std::vector<TestCase>;

// 测试夹具仅拥有本次创建且名称唯一的临时目录。
class TempDir {
  public:
    // 使用 PID、单调计时值和进程内序号建立独立目录；创建失败抛出异常，不接管已有目录。
    TempDir() {
        static std::atomic<unsigned> serial{0};
        const auto name = L"d3dx-updater-tests-" + std::to_wstring(GetCurrentProcessId()) + L"-" +
                          std::to_wstring(GetTickCount64()) + L"-" + std::to_wstring(serial++);
        root_ = fs::temp_directory_path() / name;
        if (!fs::create_directory(root_)) {
            throw std::runtime_error("Could not create an isolated test directory");
        }
    }
    // 尝试清除目录树属性并递归删除，忽略清理错误；仅适用于本次测试控制的目录内容。
    ~TempDir() {
        std::error_code ec;
        fs::recursive_directory_iterator it(root_, fs::directory_options::skip_permission_denied, ec);
        const fs::recursive_directory_iterator end;
        for (; !ec && it != end; it.increment(ec)) {
            SetFileAttributesW(it->path().c_str(), FILE_ATTRIBUTE_NORMAL);
        }
        SetFileAttributesW(root_.c_str(), FILE_ATTRIBUTE_NORMAL);
        fs::remove_all(root_, ec);
    }
    // 禁止共享临时目录的清理责任。
    TempDir(const TempDir&) = delete;
    // 禁止赋值覆盖已拥有目录的清理责任。
    TempDir& operator=(const TempDir&) = delete;
    // 返回借用的根路径引用，其有效期不超过夹具对象。
    const fs::path& path() const {
        return root_;
    }

  private:
    fs::path root_;
};

// 创建父目录并按二进制截断写入夹具内容；打开或写入失败抛出异常，不校验路径所属范围。
inline void Write(const fs::path& path, const std::string& contents) {
    fs::create_directories(path.parent_path());
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    file.exceptions(std::ios::badbit | std::ios::failbit);
    file << contents;
}

// 按二进制读取夹具字节，打开失败抛出异常；不额外检查读取中途的流错误。
inline std::string Read(const fs::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file)
        throw std::runtime_error("Could not read fixture file");
    return {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
}

// 追加编码、路径、令牌及清单基础契约测试。
void AddCoreTests(TestList& tests);
// 追加同步、保护规则及备份恢复测试。
void AddSyncTests(TestList& tests);
// 追加进程等待、主程序启动、运行中映像替换及更新缓存保留与清理测试。
void AddProcessTests(TestList& tests);
// 追加与主程序令牌、清单、语言及日志行为保持一致的契约测试。
void AddPortTests(TestList& tests);
// 追加语言加载、回退、格式化及通用 JSON 解析测试。
void AddLanguageTests(TestList& tests);
// 追加标题栏命中区域及 DPI 缩放测试。
void AddUiTests(TestList& tests);
// 追加更新器映像校验、替换、回滚和旧映像清理测试。
void AddSelfUpdateTests(TestList& tests);
// 分派测试副本的持有映像、自身替换或模拟主程序启动模式，必要时等待测试事件。
// 返回 true 表示已处理且 result 是退出码；返回 false 时继续普通测试入口，异常向调用方传播。
bool RunFixtureMode(int argc, wchar_t** argv, int& result);

}
