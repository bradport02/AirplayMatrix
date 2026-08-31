import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property string previousLine: ""
    property string currentLine: ""
    property string nextLine: ""

    // Idle state -- no AirPlay session open. A soft radar-style pulse
    // (rings expanding and fading from a centre dot) reads as "listening"
    // without needing a device glyph or any status chrome. bridgeConnected
    // tracks the metadata pipe's own connection, not whether shairport-sync
    // itself is running -- shairport-sync only opens that pipe's write end
    // once a session actually starts (confirmed by inspecting its open file
    // descriptors while idle -- none), so the false branch below is what's
    // on screen for virtually all of normal idle time, not an actual fault.
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
            text: "Discoverable: " + app.deviceName
            color: Theme.colorTextSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 13 * Theme.uiScale
            opacity: 0.75
        }

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: app.track.bridgeConnected ? "Waiting for AirPlay" : "Waiting for connection"
            color: Theme.colorTextSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 16 * Theme.uiScale
            font.weight: Font.Medium
        }
    }

    // Now-playing state: artwork on the left, lyrics on the right. The
    // artwork sizes off the row's actual available height (already a live
    // reflection of the window size) rather than a fixed pixel cap, so it
    // scales up on a big/fullscreen window instead of staying pinned small.
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

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.spacingXs

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

            LyricsPanel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                previousLine: root.previousLine
                currentLine: root.currentLine
                nextLine: root.nextLine
                hasLyrics: app.lyrics.hasLyrics
            }
        }
    }
}
