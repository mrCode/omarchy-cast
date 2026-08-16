// Cast state in the Omarchy bar.
//
// Omarchy replaced waybar with Quickshell, so the waybar module this project
// shipped stopped being displayed at all -- the JSON was still produced, but
// nothing was reading it. This is the same indicator for the shell that
// actually runs.
//
// It polls `omarchy-cast waybar`, which is deliberately the same command the
// waybar module uses: it returns {text, tooltip, class} and NEVER spawns the
// daemon, so polling it cannot itself start anything or keep a daemon alive.

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

BarWidget {
  id: root
  moduleName: "omarchy-cast.indicator"

  // "idle" | "connecting" | "streaming" | "failed"
  property string state: "idle"
  property string tooltip: "Not casting"

  readonly property bool casting: state === "streaming"
  readonly property bool busy: state === "connecting"
  readonly property bool failed: state === "failed"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (!statusProc.running)
      statusProc.running = true
  }

  Process {
    id: statusProc
    command: ["omarchy-cast", "waybar"]
    stdout: StdioCollector {
      onStreamFinished: {
        // A missing or half-written line must not wedge the indicator: fall
        // back to idle rather than showing a stale "streaming" forever.
        try {
          var data = JSON.parse(String(text || "").trim() || "{}")
          root.state = String(data["class"] || "idle")
          root.tooltip = String(data.tooltip || "Not casting")
        } catch (e) {
          root.state = "idle"
          root.tooltip = "Not casting"
        }
      }
    }
  }

  Timer {
    // Two seconds matches the waybar module's interval. The command is a
    // socket probe, not a scan, so this is cheap.
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar

    // Deliberately visible in every state rather than hidden when idle: a
    // toggle you cannot see is a toggle you cannot find, and this one is how
    // you stop a cast.
    text: root.casting ? "󰄡" : "󰡀"
    active: root.casting || root.busy

    tooltipText: root.tooltip + "\nLeft-click: cast menu   Right-click: stop"

    onPressed: function (b) {
      if (b === Qt.RightButton) {
        root.bar.run("omarchy-cast stop")
        root.refresh()
      } else {
        root.bar.run("omarchy-cast menu")
      }
    }
  }
}
