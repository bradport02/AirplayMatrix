import QtQuick 2.15
import QtQuick.Layouts 1.15

// Bottom transport strip: a thin scrub line (no chunky QQC2 Slider) plus
// elapsed/remaining timestamps. Fades out entirely when there's no active
// session rather than showing a frozen 0:00 bar. Always shown regardless of
// the show_lyrics/show_details toggles -- duration isn't one of the things
// this build makes optional.
Item {
    id: root
    implicitHeight: 60 * Theme.uiScale

    property real position: 0
    property real fraction: 0

    // contentReady as well as sessionActive: a session opens before there
    // is any track to describe, and a scrub bar for a track the screen
    // isn't showing yet is just a stale bar. Matches NowPlayingView.
    opacity: (app.track.sessionActive && app.track.contentReady) ? 1 : 0
    visible: opacity > 0
    Behavior on opacity { NumberAnimation { duration: Theme.durationSlow } }

    function formatTime(seconds) {
        seconds = Math.max(0, Math.floor(seconds))
        var m = Math.floor(seconds / 60)
        var s = seconds % 60
        return m + ":" + (s < 10 ? "0" : "") + s
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.spacingXl
        anchors.rightMargin: Theme.spacingXl
        anchors.topMargin: Theme.spacingSm
        anchors.bottomMargin: Theme.spacingLg
        spacing: Theme.spacingSm

        Item {
            Layout.fillWidth: true
            height: 10 * Theme.uiScale

            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width
                height: 3 * Theme.uiScale
                radius: height / 2
                color: Qt.rgba(1, 1, 1, 0.2)

                // No Behavior on width here, deliberately -- see the scrub
                // dot below for the reasoning; both used to ease and both
                // now snap.
                Rectangle {
                    id: fill
                    height: parent.height
                    radius: height / 2
                    color: Theme.colorTextPrimary
                    width: parent.width * Math.min(1, Math.max(0, root.fraction))
                }
            }

            // Snaps to each position sample instead of easing between them.
            // Main.qml polls position every 250ms, so an easing Behavior
            // here is re-triggered before the previous one finishes -- i.e.
            // something is animating for the entire duration of every
            // track. Qt Quick redraws the whole scene on any frame where
            // anything animates, so that one easing curve was enough to
            // hold this 1920x1080 window at a continuous 60fps
            // recomposite (through Xwayland *and* labwc) for the whole
            // song. What it was smoothing: roughly two pixels per quarter
            // second on a ~1800px-wide bar. Snapping is visually
            // indistinguishable at that rate and lets the scene go
            // completely idle between samples.
            // Off by default, toggled from the web UI's dashboard (see
            // display_settings.py's progress_dot_enabled). `visible: false`
            // isn't just a hidden pixel here -- Qt Quick skips invisible
            // items entirely, so with the dot off there is nothing whose x
            // has to be recomputed and repainted on every position sample;
            // the bar's own fill is a single width change on a rectangle
            // that is already there.
            Rectangle {
                visible: app.settings.progressDotEnabled
                width: 10 * Theme.uiScale
                height: width
                radius: width / 2
                color: Theme.colorTextPrimary
                anchors.verticalCenter: parent.verticalCenter
                x: Math.max(0, Math.min(parent.width - width, fill.width - width / 2))
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Text {
                text: root.formatTime(root.position)
                color: Theme.colorTextSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 12 * Theme.uiScale
            }
            Item { Layout.fillWidth: true }
            Text {
                text: root.formatTime(app.track.duration)
                color: Theme.colorTextSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 12 * Theme.uiScale
            }
        }
    }
}
