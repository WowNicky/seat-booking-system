import streamlit as st
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta
import re
from streamlit_autorefresh import st_autorefresh
from gspread.exceptions import GSpreadException
import streamlit.components.v1 as components

# =============================
# ===== CONFIGURATION =====
# =============================
SHEET_NAME = "Event_Seats"
SEATS_WS_NAME = "Seats"
WHITELIST_WS_NAME = "Whitelist"

# Malaysia = UTC+8
MYT = timezone(timedelta(hours=8))
OPEN_AT = datetime(2025, 9, 22, 8, 0, 0, tzinfo=MYT)   # <<< your opening time
CUTOFF_DATETIME = datetime(2025, 10, 4, 0, 0, 0, tzinfo=MYT)
AUTO_REFRESH_MS_BEFORE = 1000  # 1s refresh before open (countdown)
AUTO_REFRESH_MS_AFTER  = 2000  # 2s refresh after open (live seat updates)

# =====================
# ====== AUTH =========
# =====================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Load service account from Streamlit secrets
creds_dict = st.secrets["gcp_service_account"]

# Create credentials object
creds = ServiceAccountCredentials.from_json_keyfile_dict(dict(creds_dict), scope)

# Authorize with Google Sheets
client = gspread.authorize(creds)

try:
    sh = client.open(SHEET_NAME)
    seats_ws = sh.worksheet(SEATS_WS_NAME)
    wl_ws    = sh.worksheet(WHITELIST_WS_NAME)
    
except Exception as e:
    st.error(f"⚠️ Could not open Google Sheet/worksheets. Details: {e}")
    st.stop()

# =========================
# ====== UI THEME =========
# =========================
st.set_page_config(page_title="Seat Selection", layout="wide")
st.markdown(
    """
    <style>
    .stApp::before {
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background-image: url("logo.png");
        background-size: 400px;
        background-repeat: no-repeat;
        background-position: center;
        opacity: 0.08;
        z-index: 0;
    }
    .stApp > * {
        position: relative;
        z-index: 1;
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("🎟 Seat Selection System")

# =================================
# ====== HELPERS / UTILITIES ======
# =================================
def now_myt():
    return datetime.now(MYT)

def normalize_name(x: str) -> str:
    """Lowercase, strip, remove all spaces and special chars for flexible matching."""
    return re.sub(r"[^a-z0-9]", "", str(x).lower())

# =============================
# ====== SEAT FUNCTIONS =======
# =============================
@st.cache_data(ttl=30)
def get_seats():
    """Fetch all seats (cached, for UI display)."""
    rows = seats_ws.get_all_records()
    records = []
    for i, r in enumerate(rows, start=2):
        reserved_by = str(r.get("ReservedBy", "")).strip()
        phone = str(r.get("PhoneNo", "")).strip()
        status = str(r.get("Status", "")).strip().lower()
        if not reserved_by and not phone:
            r["Status"] = "available"
        elif status != "reserved":
            r["Status"] = "reserved"
        r["_row"] = i  # save sheet row for later update
        records.append(r)
    return records

def get_seat_row(seat_id):
    """Fetch the latest seat status directly from Sheets for a single seat."""
    # Find the row from cached seat map (fast, local)
    all_seats = st.session_state.get("seats_cache", [])
    seat_info = next((s for s in all_seats if s["SeatID"] == seat_id), None)
    if not seat_info:
        return None
    
    row_number = seat_info["_row"]  # cached row index
    values = seats_ws.row_values(row_number)
    values += [""] * (7 - len(values))  # pad to avoid index errors
    
    seat_id, section, r, c, status, reserved_by, phone = values[:7]
    status = status.strip().lower() if status else "available"
    
    return {
        "SeatID": seat_id,
        "Section": section,
        "Row": r,
        "Col": c,
        "Status": status,
        "ReservedBy": reserved_by,
        "PhoneNo": phone,
        "_row": row_number
    }

def update_seat_atomic(seat_info, name, phone):
    """Safely reserve a seat only if still available."""
    row_number = seat_info["_row"]

    # Re-read the row to check status
    values = seats_ws.row_values(row_number)
    values += [""] * (7 - len(values))  # pad
    status = values[4].strip().lower() if len(values) >= 5 else "available"

    if status == "reserved":
        return False  # someone else already took it

    try:
        seats_ws.update(
            f"E{row_number}:G{row_number}",
            [["reserved", name, phone]]
        )
        return True
    except Exception as e:
        st.error(f"⚠️ Could not update seat {seat_info['SeatID']}: {e}")
        return False

# =============================
# ====== WHITELIST HELPERS =====
# =============================
@st.cache_data(ttl=10)
def load_whitelist_all():
    values = wl_ws.get_all_values()
    if not values:
        return [], [], {}
    headers = values[0]
    rows = values[1:]
    hmap = {h.strip().lower(): i + 1 for i, h in enumerate(headers)}
    return headers, rows, hmap

def find_whitelist_entry(name, receipt):
    headers, rows, hmap = load_whitelist_all()
    if not headers:
        return None, None
    idx_name    = hmap.get("name")
    idx_rcp     = hmap.get("receiptno")
    idx_allowed = hmap.get("ticketsallowed")
    idx_used    = hmap.get("ticketsused")
    idx_contact = hmap.get("contact")

    want_name = normalize_name(name)
    want_rcp  = str(receipt).strip()

    for i, row in enumerate(rows, start=2):
        row_name_raw = str(row[idx_name - 1]) if idx_name else ""
        row_names = [normalize_name(n) for n in row_name_raw.split("/")]
        r_rcp     = str(row[idx_rcp - 1]).strip() if idx_rcp else ""

        # ✅ Match if typed name is inside sibling group AND receipt matches
        if any(want_name in rn for rn in row_names) and r_rcp == want_rcp:
            # --- Collect all rows with SAME sibling group (ignores receipt) ---
            group_rows = [
                (j+2, r) for j, r in enumerate(rows)
                if str(r[idx_name - 1]).strip() == row_name_raw
            ]

            # Combine quotas across receipts
            total_allowed = sum(int(str(r[idx_allowed - 1]).strip() or "0") for _, r in group_rows)
            total_used    = sum(int(str(r[idx_used - 1]).strip() or "0") for _, r in group_rows)

            entry = {
                "Name": row_name_raw,
                "ReceiptNo": want_rcp,
                "TicketsAllowed": total_allowed,
                "TicketsUsed": total_used,
                "Contact": row[idx_contact - 1] if idx_contact else "",
                "Unlimited": False,
                "GroupRows": [gr[0] for gr in group_rows]  # store row numbers for update later
            }
            return i, entry
    return None, None

def refresh_whitelist_by_row(row_number):
    headers, rows, hmap = load_whitelist_all()
    if not headers or row_number is None:
        return None, None, None
    idx = row_number - 2
    if idx < 0 or idx >= len(rows):
        return None, None, None
    row = rows[idx]
    return headers, row, hmap

def update_tickets_used(row_number, new_used, hmap):
    """
    Update the TicketsUsed column for one whitelist row.
    row_number = row index in the sheet (starts from 2 for first data row).
    new_used   = new tickets used value (int).
    hmap       = header mapping dict {header: col_index}.
    """
    try:
        col_used = hmap.get("ticketsused")
        if not col_used:
            st.error("⚠️ 'TicketsUsed' column not found in Whitelist sheet.")
            return False
        wl_ws.update_cell(row_number, col_used, str(new_used))
        return True
    except Exception as e:
        st.error(f"⚠️ Could not update TicketsUsed: {e}")
        return False

# =============================
# ====== RESERVATION HELPERS ===
# =============================
def get_user_reserved_seats_global(name, seats=None):
    """Return list of (row_num, SeatID) reserved under `name`."""
    if seats is None:
        seats = get_seats()
    reserved = []
    for r in seats:
        if str(r.get("ReservedBy", "")).strip() == str(name).strip():
            reserved.append((r.get("_row"), str(r.get("SeatID", "")).strip()))
    return reserved

def release_all_user_seats_global(name, seats=None):
    """Release all seats reserved under `name` in the Seats worksheet."""
    if seats is None:
        seats = get_seats()
    reserved = get_user_reserved_seats_global(name, seats)
    ops, freed = [], []
    for row_num, seatid in reserved:
        ops.append({"range": f"E{row_num}", "values": [["available"]]})
        ops.append({"range": f"F{row_num}", "values": [[""]]})
        ops.append({"range": f"G{row_num}", "values": [[""]]})
        freed.append(seatid)
    if ops:
        try:
            seats_ws.batch_update(ops)
        except Exception as e:
            st.error(f"Could not release seats: {e}")
            return []
    return freed

def change_seats_action():
    """Release all seats, update whitelist usage, reset session state, rerun."""
    seats = get_seats()
    freed = release_all_user_seats_global(st.session_state["user_name"], seats)

    # refresh whitelist row (read current sheet)
    _, row, hmap = refresh_whitelist_by_row(st.session_state.get("wl_row"))
    new_used = st.session_state.get("tickets_used", 0)
    allowed = st.session_state.get("tickets_allowed", 0)

    if row and hmap:
        try:
            used = int(str(row[hmap["ticketsused"] - 1]).strip() or "0")
        except Exception:
            used = st.session_state.get("tickets_used", 0)
        new_used = max(0, used - len(freed))
        ok = update_tickets_used(st.session_state["wl_row"], new_used, hmap)
        try:
            allowed = int(str(row[hmap["ticketsallowed"] - 1]).strip() or "0")
        except Exception:
            allowed = st.session_state.get("tickets_allowed", allowed)
    else:
        ok = True

    # Clear caches and reset session
    try:
        st.cache_data.clear()
    except Exception:
        pass

    st.session_state["confirmed"] = False
    st.session_state["selected_seats"] = []
    st.session_state["last_booked"] = []
    st.session_state["tickets_used"] = new_used
    st.session_state["tickets_allowed"] = allowed
    st.session_state["seats_cache"] = get_seats()

    if ok:
        st.success("✅ Released your seats. You can now reselect seats.")
        st.rerun()
    else:
        st.error("Released seats but failed to update ticket usage. Please contact admin.")
        st.rerun()

# ======================================
# ====== LOGIN / ACCESS CONTROL ========
# ======================================
for key, default in {
    "auth_ok": False,
    "user_name": "",
    "contact": "",
    "receipt": "",
    "wl_row": None,
    "tickets_allowed": 0,
    "tickets_used": 0,
    "unlimited": False,
    "selected_seats": [],
    "confirmed": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

if not st.session_state["auth_ok"]:
    st.subheader("Enter Your Details")
    with st.form("user_form"):
        name_input = st.text_input("Full Name (Performer name)")
        contact_input = st.text_input("Contact Number (digits only)")
        rcp_input = st.text_input("Receipt Number (exp: SR-244000)")
        submitted = st.form_submit_button("Verify")
    if submitted:
        row_no, entry = find_whitelist_entry(name_input, rcp_input)
        if not entry:
            st.error("❌ Not found. Please purchase tickets from admins before seat booking.")
            st.stop()
        st.session_state.update({
            "auth_ok": True,
            "user_name": name_input.strip(),
            "contact": contact_input.strip(),
            "receipt": rcp_input.strip(),
            "wl_row": row_no,
            "tickets_allowed": entry["TicketsAllowed"],
            "tickets_used": entry["TicketsUsed"],
            "unlimited": entry["Unlimited"],
            "selected_seats": [],
            "confirmed": False,
        })
        st.success("✅ Verified! Please review the Terms & Conditions.")
        st.rerun()
    st.stop()

# ==============================
# ===== T&C / INSTRUCTIONS =====
# ==============================
if "tnc_ok" not in st.session_state:
    st.session_state["tnc_ok"] = False

if not st.session_state["tnc_ok"]:
    st.title("📜 Terms & Conditions / Important Notes")

    st.markdown(f"""
    ### Please read carefully before booking:

    1. Each ticket allows you to reserve **one seat only**.  
    2. Once confirmed, seats cannot be changed unless you press **Change Seats** to release and reselect.  
    3. **Changing seats will release all seats under your account.** If you intend to free seats, press **Change Seats** on the booking page.  
    4. If you have used all your tickets, access will be locked.  
    5. Additional tickets can only be purchased through the admin team.  
    6. The system will lock automatically once your quota is reached.  
    6. Please be considerate — do not hold seats without confirming.  
    7. Siblings can use **one account** to purchase all their tickets together.  

    ---

    ### ⚠️ Important Notes:
    - After clicking a seat, please **wait a few seconds** for the system to load and update.  
    - Avoid pressing refresh too quickly — the system auto-refreshes where needed.  
    - If your seat selection does not appear instantly, wait and try again.  
    - For any issues (quota mismatch, missing seats, etc.), please contact the admin team immediately.  
    - Pressing **Change Seats** will release *all* seats reserved under your name and will update your tickets used accordingly. Think carefully before changing — you can reselect seats afterwards, but this action will free the seats for others.

    ---

    **Note:** The event team reserves the right to adjust seating arrangements, ticket allocation, or system access if necessary to ensure fairness and smooth operation.

    """)

    st.error(f"Seat booking will close after {CUTOFF_DATETIME.strftime('%d %B %Y %H:%M')}.")
    agree = st.checkbox("I have read and agree to the above Terms & Conditions")

    if agree:
        st.session_state["tnc_ok"] = True
        st.rerun()

    st.stop()

# =========================
# ===== CUTOFF CHECK ======
# =========================
now = now_myt()  # use your helper with timezone
if now > CUTOFF_DATETIME:
    st.error(f"⛔ Seat booking has closed after {CUTOFF_DATETIME.strftime('%d %B %Y %H:%M')}.")
    st.info("You can no longer view, change, or select seats. For any changes, please contact the admin team.")
    if st.button("Logout"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()
    st.stop()

# ===============================
# ===== QUOTA & SEAT FLOW ====
# ===============================
_, row, hmap = refresh_whitelist_by_row(st.session_state.get("wl_row"))
if row and hmap:
    idx_allowed = hmap.get("ticketsallowed")
    idx_used    = hmap.get("ticketsused")
    allowed = int(str(row[idx_allowed - 1]).strip() or "0") if idx_allowed else 0
    used    = int(str(row[idx_used - 1]).strip() or "0") if idx_used else 0
    remaining = (allowed - used) if not st.session_state.get("unlimited") else 10**9
else:
    allowed   = st.session_state.get("tickets_allowed", 0)
    used      = st.session_state.get("tickets_used", 0)
    remaining = (allowed - used) if not st.session_state.get("unlimited") else 10**9

if remaining <= 0:
    # User has no remaining tickets according to sheet. Show reserved seats and allow "Change Seats".
    st.error("You have already used up all your tickets. (Access locked)")

    # get seats reserved by this user (if any)
    def get_user_reserved_seats(name):
        rows = seats_ws.get_all_records()
        reserved = []
        for i, r in enumerate(rows, start=2):
            if str(r.get("ReservedBy", "")).strip() == str(name).strip():
                reserved.append((i, str(r.get("SeatID", "")).strip()))
        return reserved

    def release_all_user_seats(name):
        reserved = get_user_reserved_seats(name)
        ops = []
        freed = []
        for row, seatid in reserved:
            ops.append({"range": f"E{row}", "values": [["available"]]})
            ops.append({"range": f"F{row}", "values": [[""]]})
            ops.append({"range": f"G{row}", "values": [[""]]})
            freed.append(seatid)
        if ops:
            try:
                seats_ws.batch_update(ops)
            except Exception as e:
                st.error(f"Could not release seats: {e}")
                return []
        return freed

    reserved = get_user_reserved_seats(st.session_state["user_name"])
    reserved_seat_ids = [s for _, s in reserved]

    if reserved_seat_ids:
        st.info("✅ Seats currently reserved under your name: " + ", ".join(reserved_seat_ids))
    else:
        st.info("No seats currently reserved under your name.")

    st.info("If you would like to purchase additional tickets, please contact the admin team before proceeding with seat booking.")

    st.warning("⚠️ Changing seats will release all seats reserved under your account and update your tickets used. Think carefully before proceeding — you can reselect seats afterwards.")
    if st.button("🔄 Change Seats", key="change_seats_btn"):
        change_seats_action()

    if st.session_state.get("auth_ok", False):
        st.markdown("---")  # separator line
        if st.button("Logout", key="logout_bottom"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

    st.stop()

# --- Dynamic quota (auto updates when selecting seats) ---
quota_left = remaining - len(st.session_state["selected_seats"])

st.markdown(
    f"""
    <div style="
        background-color:#e6f2ff;
        border:2px solid #3399ff;
        border-radius:10px;
        padding:15px;
        text-align:center;
        font-size:24px;
        font-weight:bold;
        color:#004080;
        margin:20px 0;
    ">
        🎫 Remaining Tickets: {quota_left}
    </div>
    """,
    unsafe_allow_html=True
)

with st.container():
    st.markdown('<div class="block">', unsafe_allow_html=True)
    st.write(f"**Logged in as:** {st.session_state['user_name']} — {st.session_state['contact']}  "
             , unsafe_allow_html=True)
    if st.button("Change Details"):
        for key in ["auth_ok","user_name","contact","receipt","wl_row",
                    "tickets_allowed","tickets_used","unlimited","selected_seats","confirmed"]:
            if key in st.session_state:
                del st.session_state[key]
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

from streamlit_autorefresh import st_autorefresh

# =============================
# ===== OPENING TIME GATE =====
# =============================
from streamlit_autorefresh import st_autorefresh

now = now_myt()
if now < OPEN_AT:
    st.warning(f"⏳ Seat selection opens at {OPEN_AT.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # --- JavaScript live countdown (styled like launch timer) ---
    target_ts = int(OPEN_AT.timestamp() * 1000)
    countdown_html = f"""
    <div style="text-align:center; margin-top:40px;">
        <div style="font-size:40px; font-weight:bold; color:#b22222; margin-bottom:10px;">
            🚀 Seat Booking Countdown
        </div>
        <div id="countdown" style="font-size:56px; font-weight:bold; color:#222;"></div>
    </div>
    <script>
    var target = {target_ts};
    function updateCountdown() {{
        var now = new Date().getTime();
        var distance = target - now;
        if (distance <= 0) {{
            document.getElementById("countdown").innerHTML = "🎉 OPEN!";
            setTimeout(function() {{ location.reload(); }}, 1000);
            return;
        }}
        var days = Math.floor(distance / (1000 * 60 * 60 * 24));
        var hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        var minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
        var seconds = Math.floor((distance % (1000 * 60)) / 1000);

        var text = "";
        if (days > 0) text += days + "d ";
        text += ("0" + hours).slice(-2) + "h "
              + ("0" + minutes).slice(-2) + "m "
              + ("0" + seconds).slice(-2) + "s";

        document.getElementById("countdown").innerHTML = text;
    }}
    setInterval(updateCountdown, 1000);
    updateCountdown();
    </script>
    """
    st.components.v1.html(countdown_html, height=200)

    # --- Fallback: auto-refresh every 5s until open ---
    remaining_sec = int((OPEN_AT - now).total_seconds())

    # If more than 6s left → schedule a one-time refresh at (remaining_sec - 6) seconds
    if remaining_sec > 6:
        st_autorefresh(interval=(remaining_sec - 6) * 1000, key="one_time_jump")

    # Inside last 6s → refresh every 3s
    elif 0 < remaining_sec <= 6:
        st_autorefresh(interval=3000, key="countdown_refresh")

    st.info("This page will refresh once the countdown ends. Please wait...")
    st.stop()

# ===================================================
# ======== LIVE SEAT MAP (no auto-refresh) ==========
# ===================================================
st.success("🎉 Seat selection is now open! Render seat map here...")
if "seats_cache" not in st.session_state:
    st.session_state["seats_cache"] = get_seats()
seats = st.session_state["seats_cache"]

if not seats:
    st.error("No seat data found in the sheet.")
    st.stop()

sections = sorted({str(s.get("Section", "")).strip() for s in seats if str(s.get("Section", "")).strip()})
sections.insert(0, "All Sections")
selected_section = st.selectbox("Choose Section:", sections, key="selected_section")

if selected_section == "All Sections":
    filtered_seats = seats
else:
    filtered_seats = [s for s in seats if str(s.get("Section", "")).strip() == selected_section]

if "selected_seats" not in st.session_state:
    st.session_state["selected_seats"] = []

rows = sorted({str(s.get("Row", "")).strip() for s in filtered_seats})
try:
    cols = sorted({int(str(s.get("Col", "")).strip()) for s in filtered_seats}, reverse=True)
except ValueError:
    st.error("Column values must be numeric in 'Col' column.")
    st.stop()

# Mobile orientation tip
st.info("📱 For best viewing on mobile, please rotate your phone to **landscape mode** while selecting seats.")
st.subheader(f"Select Your Seat — {selected_section}")

current_selected = st.session_state["selected_seats"]
can_select_more = (len(current_selected) < remaining)

for r in rows:
    cols_ui = st.columns(len(cols))
    for i, c in enumerate(cols):
        seat = next(
            (s for s in filtered_seats
             if str(s.get("Row", "")).strip() == str(r).strip()
             and int(str(s.get("Col", "")).strip()) == int(c)), None
        )
        if not seat:
            cols_ui[i].write("")
            continue
        label = str(seat.get("SeatID", "")).strip()
        status = str(seat.get("Status", "")).strip().lower()
        is_selected = label in current_selected
        if status == "reserved":
            cols_ui[i].button(label, key=label, disabled=True, help="Reserved")
        else:
            disabled = (not is_selected) and (not can_select_more)
            btn_label = ("✅ " if is_selected else "") + label
            if cols_ui[i].button(btn_label, key=label, disabled=disabled):
                if is_selected:
                    st.session_state["selected_seats"].remove(label)
                else:
                    if len(st.session_state["selected_seats"]) < remaining:
                        st.session_state["selected_seats"].append(label)
                st.rerun()

# ======================
# ===== CONFIRM UI =====
# ======================
if st.session_state["selected_seats"]:
    st.info(f"Selected seats: {', '.join(st.session_state['selected_seats'])}")

    # Centered confirm button only
    col1, col2, col3 = st.columns([3, 2, 3])
    with col2:
        confirm_clicked = st.button("✅ Confirm", key="confirm_btn")

    if confirm_clicked:
        # --- Refresh whitelist row (tickets allowed/used) ---
        _, row, hmap = refresh_whitelist_by_row(st.session_state.get("wl_row"))
        if not (row and hmap):
            st.error("Could not verify your ticket quota. Please try again.")
            st.stop()

        allowed = int(str(row[hmap["ticketsallowed"] - 1]).strip() or "0")
        used    = int(str(row[hmap["ticketsused"] - 1]).strip() or "0")
        fresh_remaining = allowed - used if not st.session_state.get("unlimited") else 10**9

        if len(st.session_state["selected_seats"]) > fresh_remaining:
            st.error(f"You selected {len(st.session_state['selected_seats'])} seats but only {fresh_remaining} remaining. Please deselect some seats.")
            st.stop()

        # --- New: fetch all seats once, then check locally ---
        all_seats = seats_ws.get_all_records()  # 1 API call only
        seat_map = {row["SeatID"]: row for row in all_seats}

        success_list, failed_list = [], []
        for seat_id in list(st.session_state["selected_seats"]):
            latest = seat_map.get(seat_id)
            if not latest:
                failed_list.append(seat_id)
                continue
            if latest["Status"] == "reserved":
                failed_list.append(seat_id)
            else:
                try:
                    update_seat(latest, st.session_state["user_name"], st.session_state["contact"])
                    success_list.append(seat_id)
                except Exception as e:
                    st.error(f"⚠️ Could not update {seat_id}: {e}")
                    failed_list.append(seat_id)

        if failed_list:
            st.error("❌ Some seats were already taken: " + ", ".join(failed_list))

        if len(success_list) == 0:
            st.error("❌ Booking failed. Please try again.")
            st.session_state["selected_seats"] = []
            st.session_state["seats_cache"] = get_seats()
            st.rerun()

        # --- Update tickets used immediately ---
        new_used = min(allowed, used + len(success_list))
        if update_tickets_used(st.session_state["wl_row"], new_used, hmap):
            # update session state
            st.session_state["confirmed"] = True
            st.session_state["selected_seats"] = []
            st.session_state["last_booked"] = success_list
            st.session_state["tickets_used"] = new_used
            st.session_state["tickets_allowed"] = allowed

            # Update local cache instantly
            for s in st.session_state["seats_cache"]:
                if s["SeatID"] in success_list:
                    s["Status"] = "reserved"
                    s["ReservedBy"] = st.session_state["user_name"]
                    s["PhoneNo"] = st.session_state["contact"]

            st.success(f"🎉 Booking confirmed! Your seats: {', '.join(success_list)}.")
            st.rerun()
        else:
            st.error("Booked seats but failed to update ticket usage. Contact admins.")
            st.stop()

# ==========================
# ===== AFTER CONFIRM ======
# ==========================
if st.session_state.get("confirmed", False):
    _, row, hmap = refresh_whitelist_by_row(st.session_state.get("wl_row"))
    if row and hmap:
        allowed = int(str(row[hmap["ticketsallowed"] - 1]).strip() or "0")
        used    = int(str(row[hmap["ticketsused"] - 1]).strip() or "0")
        rem     = allowed - used if not st.session_state.get("unlimited") else 10**9
    else:
        rem = 0

    # helper already used earlier; re-declare locally if needed
    def get_user_reserved_seats(name):
        rows = seats_ws.get_all_records()
        reserved = []
        for i, r in enumerate(rows, start=2):
            if str(r.get("ReservedBy", "")).strip() == str(name).strip():
                reserved.append((i, str(r.get("SeatID", "")).strip()))
        return reserved

    def release_all_user_seats(name):
        reserved = get_user_reserved_seats(name)
        ops = []
        freed = []
        for row_num, seatid in reserved:
            ops.append({"range": f"E{row_num}", "values": [["available"]]})
            ops.append({"range": f"F{row_num}", "values": [[""]]})
            ops.append({"range": f"G{row_num}", "values": [[""]]})
            freed.append(seatid)
        if ops:
            try:
                seats_ws.batch_update(ops)
            except Exception as e:
                st.error(f"Could not release seats: {e}")
                return []
        return freed

    # read currently reserved seats (live from sheet)
    reserved = get_user_reserved_seats(st.session_state["user_name"])
    reserved_ids = [s for _, s in reserved]

    if rem <= 0:
        # All tickets used
        st.success(f"🎉 Thank you {st.session_state['user_name']} — your booking is confirmed.")
        if st.session_state.get("last_booked"):
            st.info("✅ Seats booked (latest): " + ", ".join(st.session_state["last_booked"]))
        elif reserved_ids:
            st.info("✅ Seats booked: " + ", ".join(reserved_ids))
        st.info("You have used all your tickets. For additional tickets, please contact the admin team.")
        col1, col2 = st.columns([2,1])
        with col1:
            if st.button("🔄 Change Seats"):
                freed = release_all_user_seats(st.session_state["user_name"])
                # update whitelist used count (reduce by number freed)
                _, row, hmap = refresh_whitelist_by_row(st.session_state.get("wl_row"))
                if row and hmap:
                    used = int(str(row[hmap["ticketsused"] - 1]).strip() or "0")
                    new_used = max(0, used - len(freed))
                    update_tickets_used(st.session_state["wl_row"], new_used, hmap)
                # reset session and go back to selection
                st.session_state["confirmed"] = False
                st.session_state["selected_seats"] = []
                st.session_state["last_booked"] = []
                st.session_state["seats_cache"] = get_seats()
                st.success("Released your seats. You can now reselect seats.")
                st.rerun()
        with col2:
            if st.button("Logout"):
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
        st.stop()
    else:
        # still has tickets remaining
        if st.session_state.get("last_booked"):
            st.success(f"🎉 Booking confirmed! Your seats: {', '.join(st.session_state['last_booked'])}.")
        elif reserved_ids:
            st.success(f"🎉 Booking confirmed! Seats booked: {', '.join(reserved_ids)}.")
        else:
            st.success("🎉 Booking confirmed!")
        # Center the Change Seats button
        st.markdown("<div style='text-align:center;'>", unsafe_allow_html=True)
        if st.button("🔄 Change Seats", key="change_seats_center"):
            freed = release_all_user_seats(st.session_state["user_name"])
            _, row, hmap = refresh_whitelist_by_row(st.session_state.get("wl_row"))
            if row and hmap:
                used = int(str(row[hmap["ticketsused"] - 1]).strip() or "0")
                new_used = max(0, used - len(freed))
                update_tickets_used(st.session_state["wl_row"], new_used, hmap)
            st.session_state["confirmed"] = False
            st.session_state["selected_seats"] = []
            st.session_state["last_booked"] = []
            st.session_state["seats_cache"] = get_seats()
            st.success("Released your seats. You can now reselect seats.")
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

# ==========================
# ===== LOGOUT BUTTON ======
# ==========================
if st.session_state.get("auth_ok", False):
    st.markdown("---")  # separator line
    # Styled button
    if st.button("Logout", key="logout_bottom"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        # replaced experimental API with stable API
        st.rerun()





