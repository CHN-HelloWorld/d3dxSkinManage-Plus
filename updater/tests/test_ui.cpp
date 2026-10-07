#include "test_support.h"
#include "ui_render.h"

namespace tests {
void AddUiTests(TestList& tests) {
    tests.push_back({"ui.caption_drags_without_dragging_the_content", [] {
                         CHECK(updater::HitTestUpdaterCaption({100, 15}, 96) == HTCAPTION);
                         CHECK(updater::HitTestUpdaterCaption({100, 100}, 96) == HTCLIENT);
                     }});
    tests.push_back({"ui.disabled_close_area_is_not_a_caption_drag_target", [] {
                         CHECK(updater::HitTestUpdaterCaption({600, 15}, 96) == HTCLIENT);
                         CHECK(updater::HitTestUpdaterCaption({560, 15}, 96) == HTCAPTION);
                     }});
    tests.push_back({"ui.caption_hit_testing_scales_with_display_dpi", [] {
                         CHECK(updater::HitTestUpdaterCaption({200, 45}, 192) == HTCAPTION);
                         CHECK(updater::HitTestUpdaterCaption({1200, 30}, 192) == HTCLIENT);
                         CHECK(updater::HitTestUpdaterCaption({200, 70}, 192) == HTCLIENT);
                     }});
}
}
