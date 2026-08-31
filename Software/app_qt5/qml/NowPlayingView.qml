import QtQuick 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    property string previousLine: ""
    property string currentLine: ""
    property string nextLine: ""

    // Idle state -- no AirPlay session open. Deliberately static: this is
    // the screen that's on-air the most (sitting there waiting for a
    // session to start), so on the Zero WH's single ARM1176 core it's the
    // one that most needs to cost the GPU nothing at all -- no rings, no
    // pulsing dot, no Behavior-driven animation, just plain white text on
    // the flat black idle backdrop (Background.qml/Theme.colorIdleBackground).
    // bridgeConnected tracks the metadata pipe's own connection, not
    // whether shairport-sync itself is running -- in practice that pipe
    // only reports connected right as a session starts, so the false
    // branch is what's on screen for virtually all of normal idle time,
    // not an actual fault. "Waiting for connection" reflects that; a
    // genuinely offline receiver isn't something this app can tell apart
    // from a receiver that's simply idle, so it doesn't try to.
    ColumnLayout {
        anchors.centerIn: parent
        visible: !app.track.sessionActive
        spacing: Theme.spacingXs

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "Discoverable: " + app.deviceName
            color: Theme.colorTextSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 14 * Theme.uiScale
        }

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: app.track.bridgeConnected ? "Waiting for AirPlay" : "Waiting for connection"
            color: Theme.colorTextPrimary
            font.family: Theme.fontFamily
            font.pixelSize: 20 * Theme.uiScale
            font.weight: Font.Medium
        }
    }

    // Now-playing state: artwork on the left, details/lyrics on the right.
    // The artwork sizes off the row's actual available height (already a
    // live reflection of the window size) rather than a fixed pixel cap, so
    // it scales up on a big/fullscreen window instead of staying pinned
    // small.
    RowLayout {
        anchors.fill: parent
        anchors.margins: Theme.spacingXl
        visible: app.track.sessionActive
        spacing: Theme.spacingXl

        AlbumArt {
            Layout.preferredWidth: parent.height
            Layout.preferredHeight: Layout.preferredWidth
            Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.alignment: Qt.AlignVCenter
            spacing: Theme.spacingLg

            // Title/artist/album -- the "song details" web UI toggle
            // (Software/display_settings.py, default on). Layouts skip
            // invisible children entirely, so hiding this just lets
            // LyricsPanel below take the freed space rather than leaving a
            // blank gap.
            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs
                visible: app.settings.showDetails

                Text {
                    text: app.track.title || "—"
                    color: Theme.colorTextPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 28 * Theme.uiScale
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: [app.track.artist, app.track.album].filter(function (s) { return s.length > 0 }).join(" — ")
                    color: Theme.colorTextSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 16 * Theme.uiScale
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    visible: text.length > 0
                }
            }

            // Lyrics -- the "lyrics" web UI toggle (default off). Fully
            // absent from the tree when off, not just hidden: LyricsPanel
            // is the CPU-heaviest thing this app does (per-frame scroll/
            // reflow), which is the whole reason this toggle exists on the
            // Zero WH build. LyricsController itself also stops fetching
            // when this is off (see app_qt5/lyrics_controller.py), so
            // turning it off is a clean, complete stop, not just a hidden
            // panel still doing the work behind it.
            LyricsPanel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: app.settings.showLyrics
                previousLine: root.previousLine
                currentLine: root.currentLine
                nextLine: root.nextLine
                hasLyrics: app.lyrics.hasLyrics
            }
        }
    }
}
