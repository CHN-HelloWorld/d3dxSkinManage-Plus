#include "test_support.h"
#include <iostream>

int wmain(int argc, wchar_t** argv) {
    int fixtureResult = 0;
    if (tests::RunFixtureMode(argc, argv, fixtureResult))
        return fixtureResult;
    tests::TestList cases;
    tests::AddCoreTests(cases);
    tests::AddSyncTests(cases);
    tests::AddProcessTests(cases);
    tests::AddPortTests(cases);
    tests::AddLanguageTests(cases);
    tests::AddUiTests(cases);
    tests::AddSelfUpdateTests(cases);
    const std::wstring filter = argc == 3 && std::wstring(argv[1]) == L"--filter" ? argv[2] : L"";
    size_t passed = 0;
    size_t failed = 0;
    for (const auto& test : cases) {
        const std::wstring name(test.name.begin(), test.name.end());
        if (!filter.empty() && name.find(filter) == std::wstring::npos)
            continue;
        try {
            test.run();
            ++passed;
            std::cout << "PASS " << test.name << '\n';
        } catch (const std::exception& error) {
            ++failed;
            std::cerr << "FAIL " << test.name << ": " << error.what() << '\n';
        }
    }
    std::cout << "Results: " << passed << " passed, " << failed << " failed\n";
    return failed || !passed ? 1 : 0;
}
