import QtQuick 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    property string previousLine: ""
    property string currentLine: ""
    property string nextLine: ""

    // Idle state -- no AirPlay session open. A soft radar-style pulse
    // (rings expanding and fading from a centre dot) reads as "listening"
    // without needing a device glyph or any status chrome; the wording
    // below is the only thing that changes between "waiting" and "offline".
    ColumnLayout {
        anchors.centerIn: parent
        visible: !app.track.sessionActive
        spacing: Theme.spacingXl

        Item {
            Layout.alignment: Qt.AlignHCenter
            implicitWidth: 96 * Theme.uiScale
            implicitHeight: implicitWidth

            Repeater {
                model: 3
                delegate: Rectangle {
                    id: ring
                    anchors.centerIn: parent
                    width: parent.width
                    height: width
                    radius: width / 2
                    color: "transparent"
                    border.width: 1.5 * Theme.uiScale
                    border.color: Theme.colorTextSecondary
                    scale: 0.35
                    opacity: 0

                    SequentialAnimation {
                        loops: Animation.Infinite
                        running: app.track.bridgeConnected
                        PauseAnimation { duration: index * 700 }
                        ParallelAnimation {
                            NumberAnimation { target: ring; property: "scale"; from: 0.35; to: 1.0; duration: 2100; easing.type: Easing.OutCubic }
                            SequentialAnimation {
                                NumberAnimation { target: ring; property: "opacity"; from: 0; to: 0.45; duration: 300 }
                                NumberAnimation { target: ring; property: "opacity"; to: 0; duration: 1800; easing.type: Easing.InCubic }
                            }
                        }
                        PauseAnimation { duration: (2 - index) * 700 }
                    }
                }
            }

            Rectangle {
                anchors.centerIn: parent
                width: 8 * Theme.uiScale
                height: width
                radius: width / 2
                color: app.track.bridgeConnected ? Theme.colorTextPrimary : Theme.colorTextSecondary
                Behavior on color { ColorAnimation { duration: Theme.durationBase } }
            }
        }

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: app.track.bridgeConnected ? "Waiting for AirPlay" : "Receiver Offline"
            color: Theme.colorTextSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 16 * Theme.uiScale
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
