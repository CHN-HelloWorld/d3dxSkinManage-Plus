#include "json.h"
#include "language.h"
#include "test_support.h"

namespace tests {
using namespace updater;

void AddLanguageTests(TestList& tests) {
    tests.push_back({"language.reads_selected_pack_and_freezes_before_replacement", [] {
                         TempDir temp;
                         const auto pack = temp.path() / L"config/languages/de-DE/strings.json";
                         Write(temp.path() / L"config/settings.json", "{\"general\":{\"language\":\"de_DE\"}}");
                         Write(pack,
                               u8"{\"messages\":{\"准备中…\":\"Vorbereitung\",\"替换: {rel}\":\"Ersetzen: {rel}\"}}");
                         InitializeLanguage(temp.path());
                         Write(pack, "{}");
                         CHECK(Tr(L"准备中…") == L"Vorbereitung");
                         CHECK(Tr(L"替换: {rel}", {{L"rel", L"中文/{file}.dll"}}) == L"Ersetzen: 中文/{file}.dll");
                     }});
    tests.push_back({"language.fallback_chain_and_invalid_fields_use_original_text", [] {
                         TempDir temp;
                         Write(temp.path() / L"config/settings.json", "{\"general\":{\"language\":\"de-DE\"}}");
                         Write(
                             temp.path() / L"config/languages/de-DE/strings.json",
                             u8"{\"_meta\":{\"fallback\":\"en-US\"},\"messages\":{\"替换: {rel}\":\"wrong {path}\"}}");
                         Write(temp.path() / L"config/languages/en-US/strings.json", u8"{\"准备中…\":\"Preparing\"}");
                         InitializeLanguage(temp.path());
                         CHECK(Tr(L"准备中…") == L"Preparing");
                         CHECK(Tr(L"替换: {rel}", {{L"rel", L"app.dll"}}) == L"替换: app.dll");
                         Write(temp.path() / L"config/languages/de-DE/strings.json", "broken JSON");
                         InitializeLanguage(temp.path());
                         CHECK(Tr(L"准备中…") == L"准备中…");
                     }});
    tests.push_back({"language.json_handles_unicode_and_rejects_ambiguous_input", [] {
                         const auto value = ParseJson(L"\ufeff{\"a\":\"\\ud83d\\ude80\",\"b\":[true,null,-1.5e2]}");
                         CHECK(value.Find(L"a")->text == L"🚀");
                         CHECK(value.Find(L"b")->array.size() == 3);
                         for (const auto* invalid :
                              {L"{\"a\":1,\"a\":2}", L"[1,]", L"01", L"\"\\ud800\"", L"{\"a\":NaN}"}) {
                             CheckThrows([&] { ParseJson(invalid); });
                         }
                     }});
}
}
