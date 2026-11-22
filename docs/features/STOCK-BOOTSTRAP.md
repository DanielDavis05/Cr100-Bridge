# Install custom firmware on a stock CR100

Open **Controllers → Firmware → Install custom firmware** for a stock CR100
running 4.3.2. The guided dialog offers the bundled R15 package when present;
an advanced option accepts another `.upd` file and an optional public key. The
bundled development key matches the CFW project builds. Installation does not
require a release manifest or a host-side package allowlist. Controllers
already reporting custom firmware should use Online Updates for later releases.
If a controller still runs an earlier custom release such as R14, use Online
Updates even when the bridge labels it Stock: that label is an assumption made
when no recent custom firmware report has arrived.

The bridge checks the selected controller's MAC, stock software version, and
household before starting. It advertises one temporary **Install firmware** room, hides the
normal bridge rooms **for that controller only**, and waits until the controller
reports only the artwork room. At the first prompt, check the physical Rooms
screen, enter that room's Now Playing screen, and choose **I am on Now Playing**.
That confirmation arms one bounded album art response to the selected controller.

When temporary root access is available, the bridge withdraws the artwork room
with SSDP byebye, restores this controller's prior room selection, and waits for
its normal cached rooms to return. It then takes fresh NAND and file captures,
runs the stock updater in check-only mode with the selected public key, and shows
the package SHA-256 and destination bank and generation. The updater's check is
performed **after** the artwork trigger; it checks whether that updater would
accept the package. Only **Confirm flash**, followed by the browser confirmation,
launches `/bin/upgrade` once.

If preparation or a prompt times out, the bridge removes the temporary room and
restores the previous selection. An interrupted session also has a durable room
recovery record so service restart can restore the view. Once the upgrade launch
has been sent, the bridge never retries it automatically. An uncertain result
requires inspecting the controller and upgrade log before another attempt.

The bridge identifies installed custom firmware from a fresh report from the
paired controller. Self-describing builds send a project-signed release label and
build ID in `CR100_CFWRelease`. The bridge can recognize the running release
before anyone uploads its package. Earlier releases use `CR100_CFWBuild` and a
recognized build ID. The controller reports every 60 seconds while its UI runs.
The observation
expires after 240 seconds. When there is no fresh report, the bridge assumes
**Stock**. A controller running custom firmware while asleep or unreachable
may therefore appear as Stock until it reports again. A previous successful
installation or a 4.3.2 version string does not override this default.

The stock 4.3.2 physical controller test showed that the normal rooms disappeared
when the temporary room was isolated, then returned after its byebye and the
selection restore, all without rebooting. R14 was subsequently installed,
rolled back to stock, and installed again through this flow. R15 still needs a
physical install acceptance run.
