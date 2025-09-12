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
OPEN_AT = datetime(2025, 9, 10, 18, 55, 0, tzinfo=MYT)   # <<< your opening time
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
    seats_ws = client.open(SHEET_NAME).worksheet(SEATS_WS_NAME)
    wl_ws    = client.open(SHEET_NAME).worksheet(WHITELIST_WS_NAME)
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

@st.cache_data(ttl=20)
def get_seats():
    records = seats_ws.get_all_records()
    for r in records:
        reserved_by = str(r.get("ReservedBy", "")).strip()
        phone = str(r.get("PhoneNo", "")).strip()
        status = str(r.get("Status", "")).strip().lower()
        if not reserved_by and not phone:
            r["Status"] = "available"
        elif status != "reserved":
            r["Status"] = "reserved"
    return records

def update_seat_atomic(seat_id, name, phone):
    try:
        cell = seats_ws.find(seat_id)
    except GSpreadException as e:
        st.error(f"Google Sheets error: {e}")
    row = cell.row
    current_status = str(seats_ws.cell(row, 5).value).strip().lower()
    if current_status not in ["", "available"]:
        return False
    seats_ws.batch_update([
        {"range": f"E{row}", "values": [["reserved"]]},
        {"range": f"F{row}", "values": [[name]]},
        {"range": f"G{row}", "values": [[phone]]},
    ])
    return True

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
    st.title("📜 Terms & Conditions / Instructions")

    st.markdown("""
    ### Please read carefully before booking:
    1. Each ticket allows you to reserve one seat only.
    2. Once confirmed, seats cannot be changed. Please check before confirming.
    3. If you have used all your tickets, access will be locked.
    4. Additional tickets can only be purchased through the admin team.
    5. The system will lock automatically after quota is reached.
    6. Please be considerate — do not hold seats without confirming.
    7. Siblings can use **one account** to purchase all their tickets together.

    ---
    **Note:** The event team reserves the right to adjust seating arrangements, ticket allocation, or system access if necessary to ensure fairness and smooth operation.
    """)

    agree = st.checkbox("✅ I have read and agree to the above Terms & Conditions")

    if st.button("Proceed to Seat Selection", disabled=not agree):
        st.session_state["tnc_ok"] = True
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
    st.error("You have already used up all your tickets. (Access locked)")
    st.info("If you would like to purchase additional tickets, please contact the admin team before proceeding with seat booking.")
    if st.button("Logout"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
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

# =================================
# ===== OPENING TIME GATE =====
# =================================
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

    st.info("This page will refresh once the countdown ends. Please wait...")
    st.stop()

# ===================================================
# ======== LIVE SEAT MAP (no auto-refresh) ==========
# ===================================================
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
    col1, col2, col3 = st.columns([3,2,3])
    with col2:
        c1, c2 = st.columns(2)
        confirm_clicked = c1.button("✅ Confirm")
        reconsider_clicked = c2.button("🔄 Reconsider")

    if confirm_clicked:
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
        fresh_seats = get_seats()
        success = 0
        failed_list = []
        for seat_id in list(st.session_state["selected_seats"]):
            seat = next((s for s in fresh_seats if s["SeatID"] == seat_id), None)
            if not seat or str(seat.get("Status", "")).strip().lower() == "reserved":
                failed_list.append(seat_id)
            else:
                ok = update_seat_atomic(seat_id, st.session_state["user_name"], st.session_state["contact"])
                if ok:
                    success += 1
                else:
                    failed_list.append(seat_id)
        if failed_list:
            st.error("❌ Some seats were already taken: " + ", ".join(failed_list))
        if success == 0:
            st.error("❌ Booking failed. Please try again.")
            st.session_state["selected_seats"] = []
            st.session_state["seats_cache"] = get_seats()
            st.rerun()
        new_used = min(allowed, used + success)
        if update_tickets_used(st.session_state["wl_row"], new_used, hmap):
            st.session_state["confirmed"] = True
            st.session_state["selected_seats"] = []
            st.success(f"🎉 Booking confirmed! Seats reserved: {success}")
            st.session_state["seats_cache"] = get_seats()
            st.rerun()
        else:
            st.error("Booked seats but failed to update ticket usage. Contact admins.")
            st.stop()

    if reconsider_clicked:
        st.session_state["selected_seats"] = []
        st.cache_data.clear()
        st.rerun()

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
    if rem <= 0:
        st.success(f"🎉 Thank you {st.session_state['user_name']}! Your booking is confirmed.")
        st.info("You have used all your available tickets. Access is now closed.")
        if st.button("Logout"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
        st.stop()
    else:
        st.success(f"🎉 Booking confirmed! You still have {rem} ticket(s) remaining.")
        st.info("You may continue to book the rest. Or logout now.")
        if st.button("Logout"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
        st.stop()

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

