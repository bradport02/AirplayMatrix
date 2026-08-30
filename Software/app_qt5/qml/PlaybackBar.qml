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

    opacity: app.track.sessionActive ? 1 : 0
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

                Rectangle {
                    id: fill
                    height: parent.height
                    radius: height / 2
                    color: Theme.colorTextPrimary
                    width: parent.width * Math.min(1, Math.max(0, root.fraction))
                    Behavior on width { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                }
            }

            Rectangle {
                width: 10 * Theme.uiScale
                height: width
                radius: width / 2
                color: Theme.colorTextPrimary
                anchors.verticalCenter: parent.verticalCenter
                x: Math.max(0, Math.min(parent.width - width, fill.width - width / 2))
                Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
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
