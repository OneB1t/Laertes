# C:\ProgramData\OE\macros\mcpserver_handlers.py
# -*- coding: utf-8 -*-
# Actions of the ODIS Jython TCP bridge. mcpserver.py runs the TCP server and reloads
# this file whenever it changes, so actions can be added or fixed without restarting ODIS.
#
# Every handler takes the request dict and returns a JSON-serializable dict with "ok".
# DIAG_LOCK is provided by mcpserver.py; it serializes access to the control units.
# Do NOT put MCP server code in this file.

import json
import os
import re
import threading
import time
import traceback

from org.osgi.framework import FrameworkUtil
from java.lang import Boolean, Character, Enum, Number, Throwable
from java.util import Collection, Map
from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory


# State that must outlive a reload of this file (jobs in flight, our registrations in ODIS):
# mcpserver.py injects _PERSIST. An older mcpserver.py does not; then it lasts until the next reload.
try:
    _KEEP = _PERSIST
except NameError:
    _KEEP = {}


# --- ODIS services ------------------------------------------------------------------------

def get_context():
    diag = Factory.getInstance()
    bundle = FrameworkUtil.getBundle(diag.getClass())
    return bundle.getBundleContext()


def get_entrance(context):
    ref = context.getServiceReference(
        "de.volkswagen.odis.vaudas.diag.systemtest.IDiagEntranceService"
    )

    if ref is None:
        raise Exception("IDiagEntranceService not found")

    return context.getService(ref), ref


def get_uds_service(context):
    ref = context.getServiceReference(
        "de.volkswagen.odis.vaudas.diag.ecuonboard.uds.IEcuOnBoardDiagJobServiceUds"
    )

    if ref is None:
        raise Exception("IEcuOnBoardDiagJobServiceUds not found")

    return context.getService(ref), ref


def get_ecu(index):
    context = get_context()
    entrance, entrance_ref = get_entrance(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()

        index = int(index)

        if index < 0 or index >= len(ecus):
            raise Exception("ECU index out of range: %d" % index)

        return ecus.get(index)

    finally:
        context.ungetService(entrance_ref)


def java_object_info(obj, include_methods=True):
    result = {
        "repr": str(obj)
    }

    try:
        clazz = obj.getClass()
        result["class"] = clazz.getName()

        if include_methods:
            methods = []

            for method in clazz.getMethods():
                methods.append(method.getName())

            result["methods"] = sorted(set(methods))

    except Exception as e:
        result["error"] = str(e)

    return result


# --- Converting API results to JSON -------------------------------------------------------

MAX_DEPTH = 6


def to_data(obj, depth=0, seen=None):
    """Turns a result of the ODIS automation API into plain JSON data.

    Java beans become dicts of their get*/is* properties, collections become lists,
    byte arrays become hex strings. Recursion stops at MAX_DEPTH and on cycles.
    """
    if obj is None or isinstance(obj, (bool, int, long, float, basestring)):
        return obj
    if isinstance(obj, (Boolean, Number, Character)):
        return obj.toString() if isinstance(obj, Character) else obj
    if isinstance(obj, Enum):
        return obj.name()
    if hasattr(obj, "typecode") and getattr(obj, "typecode", None) == "b":  # Java byte[]
        return "".join("%02X" % (b & 0xFF) for b in obj)
    if isinstance(obj, (list, tuple)):
        return [to_data(x, depth + 1, seen) for x in obj]
    if isinstance(obj, Map):
        return dict((unicode(k), to_data(obj.get(k), depth + 1, seen)) for k in obj.keySet())
    if isinstance(obj, Collection):
        return [to_data(x, depth + 1, seen) for x in obj]
    if depth >= MAX_DEPTH:
        return unicode(obj)

    seen = set(seen or ())
    if id(obj) in seen:
        return unicode(obj)
    seen.add(id(obj))

    try:
        clazz = obj.getClass()
    except Exception:
        return unicode(obj)

    result = {"_type": clazz.getSimpleName()}
    names = set()
    for method in clazz.getMethods():
        name = method.getName()
        if len(method.getParameterTypes()) != 0 or name in ("getClass", "hashCode", "toString"):
            continue
        if name.startswith("get") and len(name) > 3:
            names.add((name, name[3].lower() + name[4:]))
        elif name.startswith("is") and len(name) > 2:
            names.add((name, name[2].lower() + name[3:]))
    for name, prop in sorted(names):
        try:
            result[prop] = to_data(getattr(obj, name)(), depth + 1, seen)
        except Exception as e:
            result[prop] = "<error: %s>" % e
    if len(result) == 1:
        result["value"] = unicode(obj)
    return result


# --- Connections to control units ---------------------------------------------------------

def parse_address(cmd):
    address = cmd.get("address")
    if address is None:
        raise Exception("missing address (control unit address, e.g. \"0x0001\")")
    if isinstance(address, basestring):
        return int(address, 0)
    return int(address)


def with_connection(cmd, fn):
    """Selects the control unit at cmd["address"] and returns fn(diag, handle).

    connectToEcu leaves ODIS in its default behaviour, where every diagnostic operation opens
    and closes the link itself, so there is nothing to close afterwards (closeConnection is
    only allowed after connectToEcuAndOpenConnection).
    """
    address = parse_address(cmd)
    diag = Factory.getInstance()
    DIAG_LOCK.acquire()
    try:
        handle = diag.connectToEcu(address).getConnectionHandle()
        return checked(fn(diag, handle))
    except (Exception, Throwable) as e:
        traceback.print_exc()
        return {"ok": False, "error": error_text(e), "exception_type": e.__class__.__name__}
    finally:
        DIAG_LOCK.release()


def error_text(e):
    """Message of a Python or Java exception; ODIS messages may contain non-ASCII text."""
    if isinstance(e, Throwable):
        return unicode(e.getMessage() or e.toString())
    try:
        return unicode(e)
    except Exception:
        return repr(e)


def checked(result):
    """Returns result, or an error result if it cannot be sent as JSON."""
    try:
        json.dumps(result, default=unicode)
        return result
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": "cannot encode result as JSON: %s: %s" % (e.__class__.__name__, e)}


def names_result(names, **extra):
    names = [unicode(n) for n in (names or [])]
    result = {"ok": True, "count": len(names), "names": names}
    result.update(extra)
    return result


# --- Original actions ---------------------------------------------------------------------

def handle_list_project_ecus(cmd):
    context = get_context()
    entrance, entrance_ref = get_entrance(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()
        include_methods = bool(cmd.get("include_methods", True))

        result = []

        for i in range(len(ecus)):
            ecu = ecus.get(i)
            info = java_object_info(ecu, include_methods)
            info["index"] = i
            result.append(info)

        return {
            "ok": True,
            "count": len(result),
            "ecus": result
        }

    finally:
        context.ungetService(entrance_ref)


def session_vehicle():
    """The vehicle of the current ODIS diagnostic session (holds the units ODIS identified)."""
    context = get_context()
    ref = context.getServiceReference("de.volkswagen.odis.vaudas.session.ISessionVehicleDataProvider")
    if ref is None:
        raise Exception("ISessionVehicleDataProvider not found")
    try:
        holder = context.getService(ref).supplyVehicle()
        # The holder's class is not public, so its getter is called by reflection.
        from java.lang import Class
        method = Class.getMethod(holder.getClass(), "getVehicleAsVaudasVehicle", [])
        method.setAccessible(True)
        return method.invoke(holder, [])
    finally:
        context.ungetService(ref)


def ecu_fields(ecu):
    """What ODIS currently knows about a unit; "---" where it has not identified it."""
    out = {"repr": unicode(ecu)}
    for key, getter in (("hw_part", "getHWPartNumber"), ("sw_part", "getSWPartNumber"),
                        ("hw_version", "getHWVersion"), ("sw_version", "getSWVersion"),
                        ("protocol", "getProtocol"), ("odx_variant", "getOdxVariant"),
                        ("read_state", "getReadState"), ("available", "isAvailable"),
                        ("system_name", "getSystemIdentification"), ("serial", "getSerialNumber")):
        try:
            out[key] = to_data(getattr(ecu, getter)(), MAX_DEPTH - 1)
        except (Exception, Throwable):
            out[key] = None
    try:  # stored DTCs, if ODIS has read the event memory of this unit
        out["dtc_count"] = ecu.getActualDTCs().size() if ecu.isDtcRead() else None
    except (Exception, Throwable):
        out["dtc_count"] = None
    return out


def handle_list_vehicle_ecus(cmd):
    try:
        return checked({"ok": True, "ecus": [ecu_fields(e) for e in session_vehicle().getActualECUs()]})
    except (Exception, Throwable) as e:
        traceback.print_exc()
        return {"ok": False, "error": error_text(e)}


def handle_find_ecu(cmd):
    name = cmd.get("name", "")
    context = get_context()
    entrance, entrance_ref = get_entrance(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()

        matches = []

        for i in range(len(ecus)):
            ecu = ecus.get(i)
            text = str(ecu)

            if name.lower() in text.lower():
                info = java_object_info(ecu)
                info["index"] = i
                matches.append(info)

        return {
            "ok": True,
            "query": name,
            "count": len(matches),
            "matches": matches
        }

    finally:
        context.ungetService(entrance_ref)


def handle_get_ecu_coding_info(cmd):
    ecu = get_ecu(cmd.get("index", 0))
    info = java_object_info(ecu)

    coding_info = None

    try:
        coding_info = ecu.getCodingInfo()
    except Exception as e:
        info["coding_info_error"] = str(e)

    if coding_info is not None:
        info["coding_info"] = java_object_info(coding_info)

    return {
        "ok": True,
        "ecu": info
    }


def handle_create_coding_job(cmd):
    context = get_context()
    entrance, entrance_ref = get_entrance(context)
    uds_service, uds_ref = get_uds_service(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()

        index = int(cmd.get("index", 0))

        if index < 0 or index >= len(ecus):
            return {"ok": False, "error": "ECU index out of range"}

        ecu = ecus.get(index)
        job = uds_service.createCoding(ecu)

        if job is None:
            return {
                "ok": True,
                "ecu": str(ecu),
                "job": None
            }

        return {
            "ok": True,
            "ecu": str(ecu),
            "job": java_object_info(job)
        }

    finally:
        context.ungetService(entrance_ref)
        context.ungetService(uds_ref)


def handle_get_coding_items(cmd):
    context = get_context()
    entrance, entrance_ref = get_entrance(context)
    uds_service, uds_ref = get_uds_service(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()

        index = int(cmd.get("index", 0))

        if index < 0 or index >= len(ecus):
            return {"ok": False, "error": "ECU index out of range"}

        ecu = ecus.get(index)
        job = uds_service.createCoding(ecu)

        if job is None:
            return {
                "ok": True,
                "ecu": str(ecu),
                "items": []
            }

        items = job.getItems()
        result = []

        for item in items:
            result.append(java_object_info(item))

        return {
            "ok": True,
            "ecu": str(ecu),
            "count": len(result),
            "items": result
        }

    finally:
        context.ungetService(entrance_ref)
        context.ungetService(uds_ref)


def handle_get_coding_structure(cmd):
    context = get_context()
    entrance, entrance_ref = get_entrance(context)
    uds_service, uds_ref = get_uds_service(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()

        index = int(cmd.get("index", 0))

        if index < 0 or index >= len(ecus):
            return {"ok": False, "error": "ECU index out of range"}

        ecu = ecus.get(index)
        job = uds_service.createCoding(ecu)

        if job is None:
            return {
                "ok": True,
                "ecu": str(ecu),
                "identified_systems": [],
                "text_values": []
            }

        systems = job.getIdentifiedSystems()
        values = job.getTextCodingValues()

        systems_data = []
        values_data = []

        for system in systems:
            systems_data.append(java_object_info(system))

        for value in values:
            values_data.append(java_object_info(value))

        return {
            "ok": True,
            "ecu": str(ecu),
            "job_class": job.getClass().getName(),
            "identified_system_count": len(systems_data),
            "identified_systems": systems_data,
            "text_value_count": len(values_data),
            "text_values": values_data
        }

    finally:
        context.ungetService(entrance_ref)
        context.ungetService(uds_ref)


def handle_read_coding_values(cmd):
    context = get_context()
    entrance, entrance_ref = get_entrance(context)
    uds_service, uds_ref = get_uds_service(context)

    try:
        plan = entrance.createProjectEcuPlan()
        ecus = plan.getEcuList()

        index = int(cmd.get("index", 0))

        if index < 0 or index >= len(ecus):
            return {"ok": False, "error": "ECU index out of range"}

        ecu = ecus.get(index)
        job = uds_service.createCoding(ecu)

        if job is None:
            return {"ok": False, "error": "createCoding returned null"}

        nrc_handling = bool(cmd.get("nrc_handling", False))

        job.readCodingValues(nrc_handling)

        items = job.getItems()
        values = job.getTextCodingValues()
        systems = job.getIdentifiedSystems()

        items_data = []

        for item in items:
            items_data.append(java_object_info(item))

        return {
            "ok": True,
            "ecu": str(ecu),
            "read_state": str(job.getByteReadState()),
            "item_count": len(items_data),
            "text_value_count": len(values),
            "identified_system_count": len(systems),
            "items": items_data
        }

    finally:
        context.ungetService(entrance_ref)
        context.ungetService(uds_ref)


def odis_version_text(major, minor, micro):
    """ODIS's product bundles carry its version as one number, two digits each for major, minor and
    patch, then the build: 220000.401.0 -> "22.0.0 build 401" (as odisVersion in odis.go)."""
    if major < 10000:
        return "%d.%d.%d" % (major, minor, micro)
    text = "%d.%d.%d" % (major // 10000, major // 100 % 100, major % 100)
    if minor:
        text += " build %d" % minor
    return text


def running_odis():
    """The version and product ("OE"/"OS") of the ODIS this bridge runs in, from its OSGi bundles
    (the ECU communication framework, versioned like the product in ODIS-E and ODIS-S) and its
    install folder. Either is "" when it cannot be told."""
    version = ""
    try:
        best = None
        for b in get_context().getBundles():
            name = b.getSymbolicName() or ""
            v = b.getVersion()
            if name == "de.volkswagen.odis.vaudas.ecucomframework.interface":
                best = v
                break
            if name.startswith("de.volkswagen.odis.") and v.getMajor() >= 10000 and (best is None or v.compareTo(best) > 0):
                best = v
        if best is not None:
            version = odis_version_text(best.getMajor(), best.getMinor(), best.getMicro())
    except Throwable:
        pass
    except Exception:
        pass
    product = ""
    from java.lang import System
    area = (System.getProperty("osgi.install.area") or System.getProperty("user.dir") or "").replace("/", "\\").upper()
    for p in ("OS", "OE"):
        if ("\\" + p + "\\") in area or area.endswith("\\" + p):
            product = p
    return version, product


def handle_ping(cmd):
    # Kept across reloads of this file: the running ODIS does not change under the bridge.
    if not _KEEP.get("odis"):
        _KEEP["odis"] = running_odis()
    version, product = _KEEP["odis"]
    return {
        "ok": True,
        "pong": True,
        "bridge": "odis-jython-tcp",
        "odis_version": version,
        "odis_product": product,
        "actions": sorted(HANDLERS.keys())
    }


# --- Control unit data through the ODIS automation API (IDiagnosticInterface) ---------------
# These take "address", the control unit address (e.g. "0x0001" or 1), and connect to the unit.

def handle_read_identification(cmd):
    return with_connection(cmd, lambda diag, h: {
        "ok": True, "identification": to_data(diag.readIdentification(h))})


def handle_read_coding(cmd):
    # The first entry is the master's coding, then one per slave/subsystem.
    return with_connection(cmd, lambda diag, h: {
        "ok": True, "coding": to_data(diag.readCoding(h))})


def handle_list_coding_values(cmd):
    return with_connection(cmd, lambda diag, h: names_result(diag.getShortNamesOfCodingValues(h)))


def handle_list_adaptation_channels(cmd):
    return with_connection(cmd, lambda diag, h: names_result(diag.getShortNamesOfAdaptationChannels(h)))


def handle_read_adaptation(cmd):
    channel = cmd.get("channel")
    if not channel:
        return {"ok": False, "error": "missing channel"}
    return with_connection(cmd, lambda diag, h: {
        "ok": True, "channel": channel, "adaptation": to_data(diag.readAdaptation(h, channel))})


def handle_list_measurement_groups(cmd):
    return with_connection(cmd, lambda diag, h: names_result(diag.getShortNamesOfMeasurementGroups(h)))


def handle_list_measurements(cmd):
    group = cmd.get("group")
    if not group:
        return {"ok": False, "error": "missing group"}
    return with_connection(cmd, lambda diag, h: names_result(
        diag.getShortNamesOfMeasurements(h, group), group=group))


def handle_read_measurement(cmd):
    group, measurement = cmd.get("group"), cmd.get("measurement")
    if not group or not measurement:
        return {"ok": False, "error": "missing group or measurement"}
    return with_connection(cmd, lambda diag, h: {
        "ok": True, "group": group, "measurement": measurement,
        "values": to_data(diag.readMeasurement(h, group, measurement))})


def handle_read_measurement_group(cmd):
    group = cmd.get("group")
    if not group:
        return {"ok": False, "error": "missing group"}

    def read(diag, h):
        values = []
        for name in diag.getShortNamesOfMeasurements(h, group):
            try:
                values.append(to_data(diag.readMeasurement(h, group, name)))
            except (Exception, Throwable) as e:
                values.append({"measurement": unicode(name), "error": error_text(e)})
        return {"ok": True, "group": group, "values": values}
    return with_connection(cmd, read)


def handle_read_gateway_component_list(cmd):
    # The gateway's list of control units installed in the vehicle; address defaults to 0x0019.
    cmd = dict(cmd)
    cmd.setdefault("address", "0x0019")
    return with_connection(cmd, lambda diag, h: {
        "ok": True, "components": to_data(diag.readGatewayComponentList(h))})


def handle_clear_event_memory(cmd):
    # Deletes the unit's stored DTCs, then reads the event memory again to show what is left.
    def clear(diag, h):
        diag.resetEventMemory(h)
        return {"ok": True, "cleared": True, "event_memory": to_data(diag.readEventMemory(h))}
    return with_connection(cmd, clear)


def handle_read_event_memory(cmd):
    return with_connection(cmd, lambda diag, h: {
        "ok": True, "event_memory": to_data(diag.readEventMemory(h))})


# --- ODIS database (ODX via ASAM MCD-3D), no connection to the vehicle --------------------
# ODIS keeps its ODX data in an MCD project, reached through the ECU communication framework.

def read_field(obj, name):
    """Value of a (possibly private) field; ODIS exposes the MCD server only through one."""
    from java.lang import Class
    clazz = obj.getClass()
    while clazz is not None:
        for f in Class.getDeclaredFields(clazz):
            if f.getName() == name:
                f.setAccessible(True)
                return f.get(obj)
        clazz = Class.getSuperclass(clazz)
    raise Exception("no field %s" % name)


def mcd_project():
    context = get_context()
    ref = context.getServiceReference("de.volkswagen.odis.vaudas.ecucomframework.IEcuComMngmtService")
    if ref is None:
        raise Exception("IEcuComMngmtService not found")
    try:
        framework = read_field(context.getService(ref).getInstance(), "framework")
        return framework.getMcdServer().getMcdProject()
    finally:
        context.ungetService(ref)


def mcd_items(collection):
    for i in range(collection.getCount()):
        yield collection.getItemByIndex(i)


def project_ecu_name(address):
    """Short name of the project control unit with this address, e.g. "EnginContrModul1"."""
    context = get_context()
    entrance, entrance_ref = get_entrance(context)
    try:
        for ecu in entrance.createProjectEcuPlan().getEcuList():
            m = re.search(r"id\[(0x[0-9A-Fa-f]+)\] sname\[([^\]]*)\]", unicode(ecu))
            if m and int(m.group(1), 16) == address:
                return m.group(2)
    finally:
        context.ungetService(entrance_ref)
    raise Exception("no control unit with address 0x%04X in the ODIS project" % address)


def odx_link(cmd):
    """The database logical link of the unit at cmd["address"] (UDS preferred)."""
    name = project_ecu_name(parse_address(cmd))
    links = mcd_project().getActiveDbVehicleInformation().getDbLogicalLinks()
    candidates = [n for n in links.getNames() if n.startswith("LL_" + name)]
    if not candidates:
        raise Exception("no logical link for %s in the ODIS database" % name)
    candidates.sort(key=lambda n: (not n.endswith("UDS"), n))
    return links.getItemByName(candidates[0])


def odx_location(cmd):
    """Database location to read: the ECU variant cmd["variant"] (with its base variant
    cmd["base"]) if given, otherwise the base variant of the unit at cmd["address"]."""
    variant = cmd.get("variant")
    if not variant:
        link = odx_link(cmd)
        return link.getDbLocation(), {"logical_link": link.getShortName(), "long_name": link.getLongName(),
                                      "protocol": link.getProtocolType()}
    bases = mcd_project().getDbProject().getDbEcuBaseVariants()
    names = [cmd["base"]] if cmd.get("base") else list(bases.getNames())
    for base_name in names:
        base = bases.getItemByName(base_name)
        if variant in list(base.getDbEcuVariants().getNames()):
            location = base.getDbEcuVariants().getItemByName(variant).getDbLocations().getItemByIndex(0)
            return location, {"base": base_name, "variant": variant}
    raise Exception("no ECU variant %s in the ODIS database" % variant)


def handle_odx_variants(cmd):
    """ECU variants in the database: those of the unit at "address", or every variant whose name
    contains "query" (e.g. a part number such as 04C906026BQ)."""
    def read():
        bases = mcd_project().getDbProject().getDbEcuBaseVariants()
        if cmd.get("address") is not None:
            name = project_ecu_name(parse_address(cmd))
            base_names = [n for n in bases.getNames() if n.startswith("BV_" + name)]
        else:
            base_names = list(bases.getNames())
        query = (cmd.get("query") or "").lower()
        out = []
        for base_name in base_names:
            for v in bases.getItemByName(base_name).getDbEcuVariants().getNames():
                if query in v.lower():
                    out.append({"base": base_name, "variant": v})
        return {"ok": True, "count": len(out), "variants": out[:5000]}
    return with_database(read)


def with_database(fn):
    """Runs fn() under DIAG_LOCK, for jobs that use no unit connection (database, VCI)."""
    DIAG_LOCK.acquire()
    try:
        return checked(fn())
    except (Exception, Throwable) as e:
        traceback.print_exc()
        return {"ok": False, "error": error_text(e), "exception_type": e.__class__.__name__}
    finally:
        DIAG_LOCK.release()


def mcd_value(value):
    try:
        return unicode(value.getValueAsString())
    except Exception:
        return None


def pdu_hex(value):
    """Bytes of an MCD PDU value as hex ("22F187"), "" when there are none."""
    try:
        return "".join("%02X" % (b & 0xFF) for b in value.getValueAsBytefield())
    except (Exception, Throwable):
        try:
            return re.sub(r"[^0-9A-Fa-f]", "", unicode(value.getValueAsString())).upper()
        except (Exception, Throwable):
            return ""


def handle_odx_did_layouts(cmd):
    """The ReadDataByIdentifier services of a unit's ODX variant (cmd as for odx_unit, optional
    "dids": ["2A26", ...]): each DID with its service name, request PDU and the positive response's
    minimum/maximum length and parameters, so a simulated unit can answer it in a format ODIS accepts."""
    want = set(d.upper() for d in (cmd.get("dids") or []))
    named = (cmd.get("service_query") or "").lower()  # services by name, also those without a default PDU

    def read():
        location, info = odx_location(cmd)
        out = []
        for prim in mcd_items(location.getDbDiagComPrimitives()):
            if named:
                if named not in prim.getShortName().lower():
                    continue
                try:
                    pdu = pdu_hex(prim.getDbRequest().getDefaultPDU())
                except (Exception, Throwable):
                    pdu = ""
                item = {"did": pdu[2:6], "service": prim.getShortName(), "request": pdu}
                try:
                    item["request_params"] = [param_tree(q) for q in mcd_items(prim.getDbRequest().getDbRequestParameters())]
                except (Exception, Throwable):
                    pass
                out.append(item)
                continue
            try:
                pdu = pdu_hex(prim.getDbRequest().getDefaultPDU())
            except (Exception, Throwable):
                continue  # jobs and other primitives without a request PDU
            if not pdu.startswith("22") or len(pdu) < 6 or (want and pdu[2:6] not in want):
                continue
            item = {"did": pdu[2:6], "service": prim.getShortName(), "request": pdu, "responses": []}
            for resp in mcd_items(prim.getDbResponses()):
                params = []
                for p in mcd_items(resp.getDbResponseParameters()):
                    d = {"name": p.getShortName()}
                    for key, get in (("byte_pos", p.getBytePos), ("bit_pos", p.getBitPos), ("byte_length", p.getByteLength),
                                     ("bit_length", p.getBitLength), ("min_length", p.getMinLength), ("max_length", p.getMaxLength)):
                        try:
                            d[key] = int(get())
                        except (Exception, Throwable):
                            pass
                    for key, get in (("type", p.getMCDParameterType), ("data_type", p.getDataType)):
                        try:
                            d[key] = unicode(get())
                        except (Exception, Throwable):
                            pass
                    try:
                        d["coded_default"] = mcd_value(p.getCodedDefaultValue())
                    except (Exception, Throwable):
                        pass
                    params.append(d)
                item["responses"].append({"type": unicode(resp.getResponseType()), "params": params})
            out.append(item)
        out.sort(key=lambda x: x["did"])
        result = {"ok": True, "count": len(out), "dids": out}
        result.update(info)
        return result
    return with_database(read)


def param_tree(p, depth=0):
    """A database parameter with its position, length and type, and its sub-parameters (structures,
    fields that repeat to the end of the PDU), as a dict."""
    d = {"name": p.getShortName()}
    for key, get in (("byte_pos", p.getBytePos), ("bit_pos", p.getBitPos), ("byte_length", p.getByteLength),
                     ("bit_length", p.getBitLength), ("min_length", p.getMinLength), ("max_length", p.getMaxLength)):
        try:
            d[key] = int(get())
        except (Exception, Throwable):
            pass
    for key, get in (("type", p.getMCDParameterType), ("data_type", p.getDataType), ("long_name", p.getLongName)):
        try:
            d[key] = unicode(get())
        except (Exception, Throwable):
            pass
    try:
        d["coded_default"] = mcd_value(p.getCodedDefaultValue())
    except (Exception, Throwable):
        pass
    try:
        d["variable_length"] = bool(p.isVariableLength())
    except (Exception, Throwable):
        pass
    try:  # a text table: its coded values and names (e.g. a DID number per table row)
        values = [{"text": e.getLongName(), "low": mcd_value(e.getInterval().getLowerLimit())}
                  for e in mcd_items(p.getTextTableElements())]
        if values:
            d["values"] = values
    except (Exception, Throwable):
        pass
    if depth < 6:
        try:
            subs = [param_tree(s, depth + 1) for s in mcd_items(p.getDbParameters())]
            if subs:
                d["params"] = subs
        except (Exception, Throwable):
            pass
    return d


def handle_odx_table_layout(cmd):
    """A database table's read services (request PDUs) and each row's key and full parameter tree
    (cmd as for odx_table): the layout a simulated unit must answer the table's DIDs in."""
    def read():
        location, info = odx_location(cmd)
        table = location.getDbTables().getItemByName(cmd["table"])
        services = []
        for prim in mcd_items(table.getDbDiagComPrimitives()):
            s = {"service": prim.getShortName()}
            try:
                s["request"] = pdu_hex(prim.getDbRequest().getDefaultPDU())
            except (Exception, Throwable):
                pass
            try:
                s["request_params"] = [param_tree(p) for p in mcd_items(prim.getDbRequest().getDbRequestParameters())]
                s["responses"] = [{"type": unicode(r.getResponseType()), "params": [param_tree(p) for p in mcd_items(r.getDbResponseParameters())]}
                                  for r in mcd_items(prim.getDbResponses())]
            except (Exception, Throwable):
                pass
            services.append(s)
        rows = []
        for row in mcd_items(table.getDbTableRows()):
            r = {"name": row.getShortName(), "long_name": row.getLongName(), "key": mcd_value(row.getKey())}
            try:
                r["key_coded"] = pdu_hex(row.getKey())
            except (Exception, Throwable):
                pass
            r["params"] = [param_tree(p) for p in mcd_items(row.getDbParameters())]
            rows.append(r)
        result = {"ok": True, "table": cmd["table"], "services": services, "rows": rows}
        result.update(info)
        return result
    return with_database(read)


def handle_odx_variant_patterns(cmd):
    """How ODIS identifies each ECU variant of a base variant (cmd["base"], or the unit at "address"):
    its matching patterns, each a list of {service, param, expected} that must all hold (e.g. the
    ASAM dataset name F19E and its version F1A2)."""
    def read():
        bases = mcd_project().getDbProject().getDbEcuBaseVariants()
        if cmd.get("base"):
            base_names = [cmd["base"]]
        else:
            name = project_ecu_name(parse_address(cmd))
            base_names = [n for n in bases.getNames() if n.startswith("BV_" + name)]
        out = []
        for base_name in base_names:
            for v in mcd_items(bases.getItemByName(base_name).getDbEcuVariants()):
                patterns = []
                try:
                    location = v.getDbLocations().getItemByIndex(0)
                    for pat in mcd_items(location.getDbVariantPatterns()):
                        match = []
                        for m in mcd_items(pat.getDbMatchingParameters()):
                            d = {}
                            try:
                                d["service"] = m.getDbDiagComPrimitive().getShortName()
                            except (Exception, Throwable):
                                pass
                            try:
                                d["param"] = m.getDbResponseParameter().getShortName()
                            except (Exception, Throwable):
                                pass
                            try:
                                d["expected"] = mcd_value(m.getExpectedValue())
                            except (Exception, Throwable):
                                pass
                            match.append(d)
                        patterns.append(match)
                except (Exception, Throwable) as e:
                    patterns = [{"error": error_text(e)}]
                out.append({"base": base_name, "variant": v.getShortName(), "patterns": patterns})
        return {"ok": True, "variants": out}
    return with_database(read)


def handle_gateway_nodes(cmd):
    """The gateway's node addresses per logical link ({"LL_Brake1UDS": 19, ...}), from the text table
    of its component list (TAB_RecorDataIdentGatewCompoList, row ECU identification) in the newest
    gateway variant of the project: what the virtual VCI's simulated gateway lists its units by."""
    def find(p, name):
        if p.getShortName() == name:
            return p
        for s in mcd_items(p.getDbParameters()):
            hit = find(s, name)
            if hit is not None:
                return hit
        return None

    def read():
        bases = mcd_project().getDbProject().getDbEcuBaseVariants()
        names = [n for n in bases.getNames() if n.startswith("BV_Gatew")]
        if not names:
            raise Exception("the vehicle project has no gateway (BV_Gatew...) in the ODIS database")
        variants = sorted(bases.getItemByName(names[0]).getDbEcuVariants().getNames())
        for v in reversed(variants):
            location = bases.getItemByName(names[0]).getDbEcuVariants().getItemByName(v).getDbLocations().getItemByIndex(0)
            try:
                table = location.getDbTables().getItemByName("TAB_RecorDataIdentGatewCompoList")
            except (Exception, Throwable):
                continue
            for row in mcd_items(table.getDbTableRows()):
                if row.getShortName() != "TABROW_GatewCompoListECUIdent":
                    continue
                for p in mcd_items(row.getDbParameters()):
                    links = find(p, "Param_LOGICLINKSHORTNAME")
                    if links is not None:
                        nodes = {}
                        for e in mcd_items(links.getTextTableElements()):
                            nodes[e.getLongName()] = int(mcd_value(e.getInterval().getLowerLimit()))
                        return {"ok": True, "variant": v, "nodes": nodes}
        raise Exception("no gateway variant with a component list node table")
    return with_database(read)


def handle_odx_unit(cmd):
    def read():
        location, info = odx_location(cmd)
        tables = [{"name": t.getShortName(), "long_name": t.getLongName(), "semantic": t.getSemantic(),
                   "rows": t.getDbTableRows().getCount()} for t in mcd_items(location.getDbTables())]
        result = {"ok": True, "location": location.getShortName(),
                  "location_long_name": location.getLongName(),
                  "dtc_count": location.getDbDTCs(255, 1).getCount(),
                  "tables": sorted(tables, key=lambda t: t["name"])}
        result.update(info)
        return result
    return with_database(read)


def handle_odx_dtcs(cmd):
    # getDbDTCs takes the severity levels as (highest, lowest), each 1..255.
    def read():
        location, info = odx_location(cmd)
        dtcs = [{"code": d.getDisplayTroubleCode(), "number": d.getTroubleCode(),
                 "text": d.getUnicodeDTCText() or d.getDTCText(), "level": d.getLevel()}
                for d in mcd_items(location.getDbDTCs(255, 1))]
        dtcs.sort(key=lambda d: d["number"])
        result = {"ok": True, "location": location.getShortName(), "count": len(dtcs), "dtcs": dtcs}
        result.update(info)
        return result
    return with_database(read)


def handle_odx_table(cmd):
    """Rows of a database table (e.g. TAB_RecorDataIdentCalibData = adaptations), with their
    parameters, units and selectable values."""
    table_name = cmd.get("table")
    if not table_name:
        return {"ok": False, "error": "missing table"}

    def param(p):
        # Each detail is optional: e.g. reserved parameters have no data definition at all.
        out = {"name": p.getShortName()}
        for key, get in (("long_name", p.getLongName), ("unit", p.getUnit)):
            try:
                out[key] = get() or ""
            except (Exception, Throwable):
                pass
        # Where the value sits in the record, so a coding can be decoded with this definition.
        for key, get in (("byte_pos", p.getBytePos), ("bit_pos", p.getBitPos), ("bit_length", p.getBitLength)):
            try:
                out[key] = int(get())
            except (Exception, Throwable):
                pass
        try:
            elements = list(mcd_items(p.getTextTableElements()))
            out["choices"] = [e.getLongName() for e in elements]
            out["values"] = [{"text": e.getLongName(), "low": mcd_value(e.getInterval().getLowerLimit()),
                              "high": mcd_value(e.getInterval().getUpperLimit())} for e in elements]
        except (Exception, Throwable):
            pass
        return out

    def read():
        location, info = odx_location(cmd)
        table = location.getDbTables().getItemByName(table_name)
        rows = []
        for row in mcd_items(table.getDbTableRows()):
            rows.append({"name": row.getShortName(), "long_name": row.getLongName(),
                         "key": mcd_value(row.getKey()),
                         "params": [param(p) for p in mcd_items(row.getDbParameters())]})
        result = {"ok": True, "location": location.getShortName(), "table": table_name,
                  "long_name": table.getLongName(), "count": len(rows), "rows": rows}
        result.update(info)
        return result
    return with_database(read)


TEXTUAL_TABLE = "TAB_RecorDataIdentVariaCodinWritaTextu"
_textual_cache = {}  # base variant -> family_textual() of it (reset on reload)


def textual_params(location):
    """Short names of the textual coding parameters a database location defines."""
    table = location.getDbTables().getItemByName(TEXTUAL_TABLE)
    return [p.getShortName() for row in mcd_items(table.getDbTableRows()) for p in mcd_items(row.getDbParameters())]


def family_textual(bases, base_name):
    """Each ECU variant of a base variant with its number of textual coding parameters: more than one
    is a coding definition; 0 or 1 (a "Disabled" or plain byte field) is none; -1 means the variant
    has no textual coding table."""
    if base_name not in _textual_cache:
        out = []
        for v in mcd_items(bases.getItemByName(base_name).getDbEcuVariants()):
            try:
                n = len(textual_params(v.getDbLocations().getItemByIndex(0)))
            except (Exception, Throwable):
                n = -1
            out.append({"variant": v.getShortName(), "params": n})
        _textual_cache[base_name] = out
    return _textual_cache[base_name]


def handle_coding_coverage(cmd):
    """Textual coding of every unit of the vehicle project: each ECU variant the database has for it
    (by base variant), with family_textual()'s parameter count. A base variant named after several
    units (BV_Airba… for Airba and Airba2) belongs to the longest name."""
    def read():
        context = get_context()
        entrance, entrance_ref = get_entrance(context)
        try:
            ecus = [re.search(r"id\[(0x[0-9A-Fa-f]+)\] sname\[([^\]]*)\]", unicode(e))
                    for e in entrance.createProjectEcuPlan().getEcuList()]
        finally:
            context.ungetService(entrance_ref)
        ecus = [(m.group(1), m.group(2)) for m in ecus if m]
        bases = mcd_project().getDbProject().getDbEcuBaseVariants()
        owner = {}
        for base_name in bases.getNames():
            names = [n for _, n in ecus if n and base_name.startswith("BV_" + n)]
            if names:
                owner[base_name] = max(names, key=len)
        units = [{"address": a, "sname": n,
                  "bases": [{"base": b, "variants": family_textual(bases, b)} for b in sorted(owner) if owner[b] == n]}
                 for a, n in ecus]
        return {"ok": True, "units": units}
    return with_database(read)


def handle_odx_textual_variants(cmd):
    """Variants of a unit's family (same base variant) that define textual coding, closest names to
    cmd["variant"] first. The family is given by "base" or by the unit "address"."""
    def read():
        bases = mcd_project().getDbProject().getDbEcuBaseVariants()
        if cmd.get("base"):
            base_names = [cmd["base"]]
        else:
            name = project_ecu_name(parse_address(cmd))
            base_names = [n for n in bases.getNames() if n.startswith("BV_" + name)]
        found = [dict(v, base=base_name) for base_name in base_names
                 for v in family_textual(bases, base_name) if v["params"] > 1]
        def family(name):  # GV_/EV_ prefixes differ between otherwise related variants
            return re.sub(r"^[A-Z]V_", "", name)
        ref = family(cmd.get("variant") or "")

        def common(name):
            name = family(name)
            n = 0
            while n < min(len(name), len(ref)) and name[n] == ref[n]:
                n += 1
            return n
        found.sort(key=lambda x: (-common(x["variant"]), x["variant"]))
        return {"ok": True, "count": len(found), "variants": found}
    return with_database(read)


# --- Exploration (debug) ------------------------------------------------------------------
# _debug calls methods inside ODIS by name, so it requires the token from DEBUG_TOKEN_FILE:
# local tools can read that file, a web page that posts to this port cannot.

DEBUG_TOKEN_FILE = r"C:\ProgramData\OE\macros\.mcpserver_debug_token"


def debug_token():
    import os, binascii
    if not os.path.exists(DEBUG_TOKEN_FILE):
        f = open(DEBUG_TOKEN_FILE, "w")
        f.write(binascii.hexlify(os.urandom(16)))
        f.close()
    return open(DEBUG_TOKEN_FILE).read().strip()


def invoke_declared(obj, name):
    from java.lang import Class
    clazz = obj.getClass()
    while clazz is not None:
        for m in Class.getDeclaredMethods(clazz):
            if m.getName() == name and len(m.getParameterTypes()) == 0:
                m.setAccessible(True)
                return m.invoke(obj, [])
        clazz = Class.getSuperclass(clazz)
    raise Exception("no method %s()" % name)


def field_list(clazz):
    from java.lang import Class
    out = []
    while clazz is not None:
        out += [re.sub(r"[\w$]+\.", "", f.toString()) for f in Class.getDeclaredFields(clazz)]
        clazz = Class.getSuperclass(clazz)
    return out


def handle__debug(cmd):
    """Explores ODIS objects: {"token", "service", "path": [step, ...], "param_of": [method, i]}.

    A step is a method name (no arguments), [name, arg, ...], an int (element of a collection),
    "@pkg.Class.method" (static method of a class loaded through the current object's bundle)
    "!method" (no-argument method called by reflection, for non-public classes), "^pkg.Class.FIELD"
    (static field, private ones included), ".field"
    (field read by reflection)
    or, as the last step, "#pkg.Class" (list that class's methods, static ones included).
    Returns the class, text and public methods of the object reached.
    """
    if cmd.get("token") != debug_token():
        return {"ok": False, "error": "missing or wrong debug token (see %s)" % DEBUG_TOKEN_FILE}
    from java.lang import Class
    context = get_context()
    if cmd.get("list_services") is not None:  # names of registered services containing this text
        q = cmd["list_services"].lower()
        names = set()
        for r in context.getAllServiceReferences(None, None) or []:
            names.update(n for n in r.getProperty("objectClass") if q in n.lower())
        return {"ok": True, "services": sorted(names)}
    ref = context.getServiceReference(cmd["service"])
    obj = context.getService(ref)
    try:
        listed = None
        for step in cmd.get("path", []):
            if isinstance(step, basestring) and step.startswith("#"):
                listed = FrameworkUtil.getBundle(obj.getClass()).loadClass(step[1:])
            elif isinstance(step, basestring) and step.startswith("."):
                obj = read_field(obj, step[1:])
            elif isinstance(step, basestring) and step.startswith("!"):
                obj = invoke_declared(obj, step[1:])
            elif isinstance(step, basestring) and step.startswith("^"):  # static field
                cls_name, name = step[1:].rsplit(".", 1)
                clazz = FrameworkUtil.getBundle(obj.getClass()).loadClass(cls_name)
                field = Class.getDeclaredField(clazz, name)
                field.setAccessible(True)
                obj = field.get(None)
            elif isinstance(step, basestring) and step.startswith("@"):
                cls_name, meth = step[1:].rsplit(".", 1)
                clazz = FrameworkUtil.getBundle(obj.getClass()).loadClass(cls_name)
                obj = Class.getMethod(clazz, meth, []).invoke(None, [])
            elif isinstance(step, int):
                obj = list(obj)[step]
            elif isinstance(step, list):
                obj = getattr(obj, step[0])(*step[1:])
            else:
                obj = getattr(obj, step)()
        if obj is None or isinstance(obj, (basestring, bool, int, long, float)):  # a plain value
            return {"ok": True, "class": None, "repr": unicode(obj)[:2000]}
        clazz = listed or obj.getClass()
        if cmd.get("param_of"):
            name, i = cmd["param_of"]
            clazz = [m for m in clazz.getMethods() if m.getName() == name][0].getParameterTypes()[i]
        sigs = []
        if clazz is not None:
            for m in (Class.getDeclaredMethods(clazz) if listed else Class.getMethods(clazz)):
                sig = re.sub(r"[\w$]+\.", "", m.toString().replace("public ", "").replace("abstract ", ""))
                if not re.search(r" (wait|notify|notifyAll|hashCode|equals|getClass|toString)\(", sig):
                    sigs.append(sig)
        size = None
        try:
            size = obj.size()
        except Exception:
            try:
                size = obj.getCount()
            except Exception:
                pass
        if cmd.get("names"):  # sample of an MCD collection's names
            return {"ok": True, "size": size, "names": list(obj.getNames())[:cmd["names"]]}
        return {"ok": True, "class": Class.getName(clazz) if clazz is not None else None,
                "repr": unicode(obj)[:500], "size": size, "methods": sorted(set(sigs)),
                "fields": field_list(clazz) if clazz is not None else []}
    except (Exception, Throwable) as e:
        traceback.print_exc()
        return {"ok": False, "error": error_text(e)}
    finally:
        context.ungetService(ref)


# --- Writing coding and adaptation, security access ---------------------------------------
# These change the control unit. The web UI confirms each change (old -> new) before calling.

def with_open_connection(cmd, fn):
    """Like with_connection, but keeps one link open for the whole call so a security-access
    login and the operation that needs it run on the same connection. ODIS otherwise opens and
    closes a fresh link per operation, which drops the unlock before the write."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    address = parse_address(cmd)
    diag = Factory.getInstance()
    DIAG_LOCK.acquire()  # like with_connection: one job at a time, writes included
    try:
        handle = diag.connectToEcu(address).getConnectionHandle()
        diag.openConnection(handle)
        try:
            return checked(fn(diag, handle))
        finally:
            try:
                diag.closeConnection(handle)
            except (Exception, Throwable):
                pass
    except (Exception, Throwable) as e:
        traceback.print_exc()
        return {"ok": False, "error": error_text(e), "exception_type": e.__class__.__name__}
    finally:
        DIAG_LOCK.release()


def maybe_login(diag, handle, cmd):
    """On an open connection: optional diagnostic-session switch, then security access, when the
    command carries a login value. Lets a write unlock the unit on its own connection."""
    session = cmd.get("session")
    if session:
        diag.switchSession(handle, session)
    code = cmd.get("login")
    if code is not None:
        mode = cmd.get("mode")
        if not mode:
            modes = list(diag.getShortNamesOfAccessModes(handle))
            mode = modes[0] if modes else ""
        diag.securityAccess(handle, unicode(code), mode)


def gated_connection(cmd):
    """with_open_connection when a login/session is requested (so the unlock holds through the
    write), otherwise the default per-operation with_connection."""
    return with_open_connection if (cmd.get("login") is not None or cmd.get("session")) else with_connection


def handle_write_byte_coding(cmd):
    """Writes byte coding to a subsystem. name is the subsystem (systemName from read_coding,
    e.g. "master"); coding is the hex string in the same form read_coding returns. An optional
    login (and optional mode/session) performs security access on the same connection first."""
    name = cmd.get("name") or "master"
    coding = cmd.get("coding")
    if not coding:
        return {"ok": False, "error": "missing coding hex string"}
    def run(diag, h):
        maybe_login(diag, h, cmd)
        return {"ok": True, "result": to_data(diag.writeByteCoding(h, name, coding))}
    return gated_connection(cmd)(cmd, run)


def handle_read_adaptation_values(cmd):
    """The selectable values of an adaptation channel, when it has a fixed set."""
    channel = cmd.get("channel")
    if not channel:
        return {"ok": False, "error": "missing channel"}
    return with_connection(cmd, lambda diag, h: names_result(diag.getShortNamesOfAdaptationValues(h, channel), channel=channel))


def handle_adaptation_options(cmd):
    """Enumerated options for an adaptation channel's parameters, from the writable calibration
    ODX table (matched by the channel's long name). Lets the editor offer dropdowns where a
    parameter has a fixed set of values. Resolved by address, so no vehicle session is needed."""
    channel = cmd.get("channel")
    if not channel:
        return {"ok": False, "error": "missing channel"}
    table = handle_odx_table({"address": cmd.get("address"), "variant": cmd.get("variant"),
                              "base": cmd.get("base"), "table": "TAB_RecorDataIdentCalibDataWrita"})
    if not table.get("ok"):
        return {"ok": True, "channel": channel, "params": []}
    for row in table.get("rows", []):
        if row.get("long_name") == channel or row.get("name") == channel:
            params = []
            for p in row.get("params", []):
                opts = [c.get("text") for c in (p.get("values") or []) if c.get("low") == c.get("high")]
                if opts:
                    params.append({"name": p.get("name"), "options": opts})
            return {"ok": True, "channel": channel, "params": params}
    return {"ok": True, "channel": channel, "params": []}


def handle_write_adaptation(cmd):
    """Writes an adaptation channel. values maps each parameter name (as read_adaptation shows,
    e.g. "Param_ActivStartStopFunct") to its new value."""
    from java.util import HashMap
    channel, values = cmd.get("channel"), cmd.get("values")
    if not channel or not isinstance(values, dict) or not values:
        return {"ok": False, "error": "missing channel or non-empty values map"}
    m = HashMap()
    for k, v in values.items():
        m.put(k, v)
    def run(diag, h):
        maybe_login(diag, h, cmd)
        return {"ok": True, "result": to_data(diag.writeAdaptation(h, channel, m))}
    return gated_connection(cmd)(cmd, run)


def handle_list_access_modes(cmd):
    """The named security-access modes an ECU offers (for the login value)."""
    return with_connection(cmd, lambda diag, h: names_result(diag.getShortNamesOfAccessModes(h)))


def handle_login(cmd):
    """Security access ("login"): elevated mode for coding/adaptation. securityAccess takes an
    access-mode name and a value; mode defaults to the first the ECU offers."""
    value = cmd.get("code")
    if value is None:
        value = cmd.get("value") or ""
    def run(diag, h):
        session = cmd.get("session")
        if session:
            diag.switchSession(h, session)
        modes = list(diag.getShortNamesOfAccessModes(h))
        mode = cmd.get("mode") or (modes[0] if modes else "")
        return {"ok": True, "mode": mode, "modes": [unicode(x) for x in modes],
                "result": to_data(diag.securityAccess(h, unicode(value), mode))}
    # Open the link for the whole call; note the unlock is dropped when it closes, so a coding or
    # adaptation write must carry the login itself to be unlocked on its own connection.
    return with_open_connection(cmd, run)


# --- Guided fault finding (GFS) -----------------------------------------------------------
# ODIS-S guided fault finding ("Gefuehrte Fehlersuche") hangs off an IGfs session, created for
# the vehicle and held by GfsManager. These actions report its state and, from it, the repair
# guidance (test programs, documents) for a fault, so a DTC can be shown with what to check.

def gfs_methods(obj):
    from java.lang import Class
    if obj is None:
        return None
    sigs = []
    for m in Class.getMethods(obj.getClass()):
        sig = re.sub(r"[\w$]+\.", "", m.toString().replace("public ", "").replace("abstract ", "").replace("final ", ""))
        if not re.search(r" (wait|notify|notifyAll|hashCode|equals|getClass|toString)\(", sig):
            sigs.append(sig)
    return sorted(set(sigs))


def gfs_service(context, name):
    ref = context.getServiceReference(name)
    return (context.getService(ref) if ref is not None else None), ref


def current_gfs(context):
    """The current IGfs guided-diagnosis session, or None. getGfs() is static on GfsManager
    (the ISessionGfsDataProvider impl), so it is called by reflection on that class."""
    from java.lang import Class
    mgr, ref = gfs_service(context, "de.volkswagen.odis.vaudas.session.ISessionGfsDataProvider")
    try:
        if mgr is None:
            return None
        return Class.getMethod(mgr.getClass(), "getGfs", []).invoke(None, [])
    finally:
        if ref is not None:
            context.ungetService(ref)


def handle_fault_guidance(cmd):
    """Guided-fault-finding guidance for a fault: repair documents and diagnostic objects the
    GFS search service finds for the query (a DTC code or text). Needs an active guided-diagnosis
    session (vehicle identified and Guided Fault Finding started in ODIS); the knowledge base is
    empty otherwise."""
    query = cmd.get("query") or cmd.get("dtc") or ""
    context = get_context()
    gfs = current_gfs(context)
    if gfs is None:
        return {"ok": False, "error": "No guided diagnosis session; identify the vehicle and start Guided Fault Finding in ODIS."}
    out = {"ok": True, "query": query, "gfs_class": gfs.getClass().getName()}
    search, ref = gfs_service(context, "de.volkswagen.odis.vaudas.gfs.search.IGfsSearchService")
    try:
        try:
            out["documents"] = [unicode(x)[:300] for x in (search.findDocLinks(gfs, query, True, True) or [])]
        except (Exception, Throwable) as e:
            out["documents_error"] = error_text(e)
        try:
            out["diagnostic_objects"] = [unicode(x)[:300] for x in (search.findGfsDiagnosticObjects(gfs, query) or [])]
        except (Exception, Throwable) as e:
            out["diagnostic_objects_error"] = error_text(e)
    finally:
        if ref is not None:
            context.ungetService(ref)
    return out


def handle_gfs_explore(cmd):
    """Read-only: reports whether a diagnosis session is active and, if so, the current IGfs
    object with the methods available on it and on the GFS search service. Used to design the
    fault-guidance action; takes no connection to the vehicle."""
    out = {"ok": True}
    context = get_context()
    info, ref = gfs_service(context, "de.volkswagen.odis.vaudas.diag.systemtest.IDiagnosisSessionInfoService")
    try:
        if info is not None:
            out["diagnosis_active"] = info.isDiagnosisActive()
            out["diagnosis_started"] = info.isDiagnosisStarted()
            out["diagnosis_state"] = unicode(info.getDiagnosisState())
            out["vehicle_name"] = info.getVehicleName()
            out["brand"] = info.getBrand()
    except (Exception, Throwable) as e:
        out["session_error"] = error_text(e)
    finally:
        if ref is not None:
            context.ungetService(ref)
    try:
        gfs = current_gfs(context)
        out["gfs_class"] = gfs.getClass().getName() if gfs is not None else None
        out["gfs_methods"] = gfs_methods(gfs)
        if gfs is not None:
            try:
                plan = gfs.getInitialTestPlan()
                out["plan_class"] = plan.getClass().getName() if plan is not None else None
                out["plan_methods"] = gfs_methods(plan)
            except (Exception, Throwable) as e:
                out["plan_error"] = error_text(e)
            try:
                kb = gfs.getKnowledgeBase()
                out["kb_class"] = kb.getClass().getName() if kb is not None else None
                out["kb_methods"] = gfs_methods(kb)
            except (Exception, Throwable) as e:
                out["kb_error"] = error_text(e)
    except (Exception, Throwable) as e:
        out["gfs_error"] = error_text(e)
    search, ref3 = gfs_service(context, "de.volkswagen.odis.vaudas.gfs.search.IGfsSearchService")
    try:
        out["search_methods"] = gfs_methods(search)
    finally:
        if ref3 is not None:
            context.ungetService(ref3)
    return out


# Guided functions (component protection...) need GFS's knowledge base, which ODIS builds from the
# vehicle identification (brand, model, model year, variant, engine) of a Diagnosis session. The
# macro runs without one, so the identification is made here on ODIS's own selection model.

GF_LEVELS = [  # (name, list getter, selection setter)
    ("brand", "getManufacturerList", "setSelectedManufacturer"),
    ("model", "getVehicleModelList", "setSelectedVehicleModel"),
    ("year", "getModelYearList", "setSelectedModelYear"),
    ("variant", "getVehicleVariantList", "setSelectedVehicleVariant"),
    ("engine", "getEngineTypeList", "setSelectedEngineType"),
]


# Readable labels for GFS_UI built-in controls (e.g. the acknowledge button a guided function shows).
# ODIS does not ship these in its text database, so the control's id is the only source.
_GFS_UI_LABELS = {
    "ACKNOWLEDGE_BUTTON": "Acknowledge", "CANCEL_BUTTON": "Cancel", "OK_BUTTON": "OK",
    "YES_BUTTON": "Yes", "NO_BUTTON": "No", "CONTINUE_BUTTON": "Continue",
    "REPEAT_BUTTON": "Repeat", "BACK_BUTTON": "Back", "NEXT_BUTTON": "Next", "DONE_BUTTON": "Done",
}


def _gfs_ui_label(key):
    """A readable label for a GFS_UI built-in control id (ACKNOWLEDGE_BUTTON -> "Acknowledge"), from
    the table above or, failing that, the id with its "_BUTTON" suffix dropped and words capitalised."""
    if key in _GFS_UI_LABELS:
        return _GFS_UI_LABELS[key]
    words = key.replace("_BUTTON", "").replace("_", " ").strip()
    return words.capitalize() if words else key


def odis_text(context, text_id):
    """The text of an ODIS text id in the ODIS language (guided-function texts in German, the only
    language ODIS ships them in), or the id itself."""
    if text_id is None:
        return None
    # A GFS selection item (GfsSelectionItemImpl) wraps its label as a TextId; render that label.
    if not hasattr(text_id, "getSource") and hasattr(text_id, "getTextId"):
        text_id = text_id.getTextId()
        if text_id is None:
            return None
    if text_id.getSource() == "DONT_TRANSLATE_AREA":
        return unicode(text_id.getId())
    from java.util import Locale
    factory, ref = gfs_service(context, "de.volkswagen.odis.vaudas.domainmodel.base.factory.ITranslatedTextFactory")
    try:
        text = factory.getTranslatedText(Locale.getDefault(), text_id, None)
        for getter in ("getText", "getTranslatedText"):
            if text is not None and hasattr(text, getter):
                return unicode(getattr(text, getter)())
    except (Exception, Throwable):
        pass
    finally:
        if ref is not None:
            context.ungetService(ref)
    key = unicode(text_id.getId())
    source = unicode(text_id.getSource() or "")
    try:
        text = gfs_db_text(key, source or None) or (_gfs_ui_label(key) if source == "GFS_UI" else key)
    except (Exception, Throwable):
        return _gfs_ui_label(key) if source == "GFS_UI" else key
    # Fill in the test program's variables (‹name›, see gfs_db_text) with the values it gave.
    try:
        params = text_id.getParameters() if hasattr(text_id, "getParameters") else None
        for name in (params.getNames() if params is not None else []):
            text = text.replace(u"‹%s›" % name, _param_text(params.get(name)))
    except (Exception, Throwable):
        pass
    return text


def _param_text(value):
    """The text of a guided-function text parameter (a ValueImpl): its value, or for a text value
    (<String><String textid=… textsource=…>fallback</String></String>) the German fallback text it
    carries, which is the only language ODIS ships guided-function texts in."""
    v = value.getValue() if value is not None and hasattr(value, "getValue") else value
    v = u"" if v is None else unicode(v)
    return _xml_text(v) if v.lstrip().startswith("<") else v.strip()


def gf_attribute_collection(context):
    """ODIS's vehicle-identification selection model (the one its identification dialog uses)."""
    info, ref = gfs_service(context, "de.volkswagen.odis.vaudas.diag.systemtest.IDiagnosisSessionInfoService")
    try:
        return info.getVehicleIdentificatonData().getVehicleAttributeCollection()
    finally:
        if ref is not None:
            context.ungetService(ref)


def handle_gf_identify(cmd):
    """Exploratory: identifies the vehicle for guided functions without a Diagnosis session. Lists
    the choices of each level (brand, model, year, variant, engine) and selects cmd[level] (an index)
    or the only choice, or for the model the one matching the VIN's model code. Stops at the first
    level left open. Changes only ODIS's selection, nothing in the car."""
    context = get_context()
    coll = gf_attribute_collection(context)
    if cmd.get("reset"):
        coll.clearSelection()
    coll.evaluate()
    vin_codes = [unicode(r.getVehicleClass()) for r in coll.getVinExtractionResult() or []]
    out = {"ok": True, "vin": unicode(coll.getVIN()), "vin_models": vin_codes, "levels": []}
    for name, list_getter, setter in GF_LEVELS:
        items = list(getattr(coll, list_getter)() or [])
        level = {"level": name, "choices": []}
        for i, item in enumerate(items):
            node = item.getNode() if hasattr(item, "getNode") else None
            name_id = node.getName() if node is not None else item.getNameId()
            choice = {"i": i, "text": odis_text(context, name_id)}
            if node is not None:
                choice.update(project=unicode(node.getDiagProjectName()), gfsid=unicode(node.getEquipmentNodeGfsId()),
                              vin_codes=[unicode(c) for c in node.getVinModelCodes() or []])
            level["choices"].append(choice)
        out["levels"].append(level)
        pick = cmd.get(name)
        if pick is None and len(items) == 1:
            pick = 0
        if pick is None and name == "model":
            matches = [c["i"] for c in level["choices"] if set(c.get("vin_codes", [])) & set(vin_codes)]
            pick = matches[0] if len(matches) == 1 else None
        if pick is None:
            break
        try:
            getattr(coll, setter)(items[int(pick)])
            level["selected"] = int(pick)
        except (Exception, Throwable) as e:
            level["error"] = error_text(e)
            break
    out["complete"] = bool(coll.selectionIsCompleted())
    return out


def _gfs_build(cmd, questions=None):
    """Identify the vehicle (its selection must be completed by gf_identify first) and build a GFS
    session and knowledge base for it, exactly as ODIS does when a Diagnosis starts. Returns
    (context, gfs, kb, vehicle). The equipment questions the build asks are answered from
    cmd["answers"] (question id -> bool, default no) and appended to the list `questions` when given.
    Builds only an in-memory session; it changes nothing in the car and contacts no server."""
    from java.lang import Class
    from org.python.core import Py
    context = get_context()
    coll = gf_attribute_collection(context)
    if not coll.selectionIsCompleted():
        raise Exception("vehicle identification incomplete; run gf_identify first")
    vehicle = session_vehicle()
    vehicle.setManufacturer(coll.getSelectedManufacturer())
    vehicle.setVehicleModel(coll.getSelectedVehicleModel())
    vehicle.setModelYear(coll.getSelectedModelYear())
    vehicle.setVariant(coll.getSelectedVehicleVariant())
    vehicle.setEngineType(coll.getSelectedEngineType())
    service, ref = gfs_service(context, "de.volkswagen.odis.vaudas.gfs.IGfsService")
    try:
        gfs = service.createGfs(vehicle)
    finally:
        if ref is not None:
            context.ungetService(ref)
    kb = gfs.getKnowledgeBase()
    bundle = FrameworkUtil.getBundle(kb.getClass())
    pkg = "de.volkswagen.odis.vaudas.gfs.vehicledata.knowledgebase."
    decision_type = Py.java2py(bundle.loadClass(pkg + "IVariantRuleDecision"))

    class Decision(decision_type):
        def decide(self, text_id):
            answer = bool((cmd.get("answers") or {}).get(unicode(text_id.getId()), False))
            if questions is not None:
                questions.append({"id": unicode(text_id.getId()), "text": odis_text(context, text_id), "answer": answer})
            return answer

    rules_class = bundle.loadClass(pkg + "VariantRuleEvaluationRules")
    rules = Class.getMethod(rules_class, cmd.get("rules", "all"), []).invoke(None, [])
    kb.build(rules, coll.getEquipmentTree(), Decision())
    return context, gfs, kb, vehicle


def _super_function_json(context, o):
    """A guided function object as JSON: id, name and its test programs."""
    f = {"name": odis_text(context, o.getName()), "gfsid": unicode(o.getDiagObjectGfsId()), "key": unicode(o.getDiagObjectKey())}
    try:
        f["programs"] = [{"title": odis_text(context, h.getTitle()), "key": unicode(h.getTestProgramKey())}
                         for h in o.getTestProgramHandles() or []]
    except (Exception, Throwable) as e:
        f["error"] = error_text(e)
    return f


def _norm_addr(addr):
    """A unit address normalised for comparison: upper case, no 0x, no leading zeros ("05F"->"5F")."""
    a = (addr or "").upper().replace("0X", "").lstrip("0")
    return a or "0"


def _find_cp_function(kb, unit):
    """(diagnostic object, first test-program handle) of the component-protection guided function for
    a unit address (e.g. "5F"), or (None, None). The function's gfs id is "<ADDR>_Komponentenschutz..."."""
    want = _norm_addr(unit)
    for o in kb.getAllSuperFunctionObjects(True) or []:
        gid = unicode(o.getDiagObjectGfsId())
        if "Komponentenschutz" in gid and _norm_addr(gid.split("_", 1)[0]) == want:
            handles = list(o.getTestProgramHandles() or [])
            return o, (handles[0] if handles else None)
    return None, None


def _find_function_by_key(kb, key):
    """(diagnostic object, first test-program handle) of the guided function with diag-object key
    `key` (the "key" from gf_functions), or (None, None). Used for vehicle-wide functions that are
    not tied to a unit address, such as the immobilizer (Wegfahrsicherung)."""
    for o in kb.getAllSuperFunctionObjects(True) or []:
        if unicode(o.getDiagObjectKey()) == key:
            handles = list(o.getTestProgramHandles() or [])
            return o, (handles[0] if handles else None)
    return None, None


def handle_gf_functions(cmd):
    """Identify the vehicle and list its vehicle-wide guided functions (component protection, online
    system test, ...). Needs gf_identify first. Builds only an in-memory GFS session; nothing in the
    car, no server contacted."""
    questions = []
    try:
        context, gfs, kb, vehicle = _gfs_build(cmd, questions)
    except (Exception, Throwable) as e:
        return {"ok": False, "error": error_text(e)}
    objs = list(kb.getAllSuperFunctionObjects(True) or [])[:int(cmd.get("limit", 500))]
    return {"ok": True, "questions": questions, "functions": [_super_function_json(context, o) for o in objs]}


def handle_cp_probe(cmd):
    """Read-only: locate the component-protection guided function for unit cmd["unit"] and report what
    running it needs (its test-program handle, the UiInputResult constructors, the special-action
    enum constants). Contacts no VW server and runs nothing in the car."""
    try:
        context, gfs, kb, vehicle = _gfs_build(cmd)
    except (Exception, Throwable) as e:
        return {"ok": False, "error": error_text(e)}
    out = {"ok": True, "unit": cmd.get("unit")}
    obj, handle = _find_cp_function(kb, cmd.get("unit"))
    if obj is None:
        out["error"] = "no component-protection function for unit %s" % cmd.get("unit")
    else:
        out["function"] = {"gfsid": unicode(obj.getDiagObjectGfsId()), "name": odis_text(context, obj.getName()),
                           "handle_id": unicode(handle.getHandleId()) if handle else None,
                           "handle_title": odis_text(context, handle.getTitle()) if handle else None,
                           "program_key": unicode(handle.getTestProgramKey()) if handle else None}
    bundle = FrameworkUtil.getBundle(gfs.getClass())
    uir = bundle.loadClass("de.volkswagen.odis.vaudas.gfs.execution.UiInputResult")
    out["UiInputResult_ctors"] = [re.sub(r"[\w$]+\.", "", unicode(c)) for c in uir.getDeclaredConstructors()]
    action = bundle.loadClass("de.volkswagen.odis.vaudas.gfs.execution.UiInputResult$EUserSpecialAction")
    out["EUserSpecialAction"] = [unicode(c) for c in action.getEnumConstants() or []]
    from java.lang import Class
    sig = lambda c: re.sub(r"[\w$]+\.", "", unicode(c))
    # Parameter types come back as Jython type proxies, so they are inspected through Class.
    start = [m for m in gfs.getClass().getMethods() if m.getName() == "startTestProgram"]
    out["startTestProgram"] = [[Class.getName(p) for p in m.getParameterTypes()] for m in start]
    ui = bundle.loadClass("de.volkswagen.odis.vaudas.gfs.execution.IGfsUiConnector")
    for p in [p for m in start for p in m.getParameterTypes()] + [p for m in ui.getMethods() for p in m.getParameterTypes()]:
        n = Class.getName(p)
        if n.startswith("de.volkswagen") and n not in out:
            out[n] = ([unicode(c) for c in Class.getEnumConstants(p)] if Class.isEnum(p)
                      else [sig(x) for x in Class.getDeclaredMethods(p)] + [sig(x) for x in Class.getConstructors(p)])
    out["LoginDataResponse_ctors"] = [sig(c) for c in Class.getConstructors(_login_types(context)[1])]
    out["login_handlers"] = [unicode(r.getProperty("service.ranking")) + " " + unicode(context.getService(r).getClass().getName())
                             for r in context.getServiceReferences(LOGIN_HANDLER, None) or []]
    return out


LOGIN_HANDLER = "de.volkswagen.odis.vaudas.core.services.ILoginHandlerService"


def _login_types(context):
    """ILoginHandlerService and LoginDataResponse as Jython types, loaded through the bundle of
    ODIS's own login handler. Our Jython handlers (PageLogin, ranked above ODIS's) come from no
    bundle, so a handler without one is skipped rather than taken as the top-ranked service."""
    from org.python.core import Py
    for ref in context.getServiceReferences(LOGIN_HANDLER, None) or []:
        handler = context.getService(ref)
        try:
            bundle = FrameworkUtil.getBundle(handler.getClass()) if handler is not None else None
        finally:
            context.ungetService(ref)
        if bundle is not None:
            return (Py.java2py(bundle.loadClass(LOGIN_HANDLER)),
                    Py.java2py(bundle.loadClass("de.volkswagen.odis.vaudas.core.LoginDataResponse")))
    raise Exception("ODIS has no login handler service")


def _register_page_login(context, jid, log):
    """Registers, above ODIS's own sign-in dialog (DialogLoginHandler), a login handler that asks
    the page for the online sign-in and hands it to ODIS, which logs in to VW and decides. Returns
    the registration to unregister, or None when ODIS keeps its own dialog. One left registered by
    an earlier job that never finished is unregistered first (one job runs at a time), so it does
    not keep answering ODIS's sign-in for a job that is gone."""
    from java.util import Hashtable
    from java.lang import Integer
    stale = _KEEP.pop("page_login", None)
    if stale is not None:
        try:
            stale.unregister()
        except (Exception, Throwable):
            pass  # already unregistered
    try:
        handler_type, response = _login_types(context)

        class PageLogin(handler_type):
            def performLogin(self, request):
                failure = request.getPreviousFailure()
                text = " ".join(t for t in [request.getTitle(), request.getLogonReason(), request.getLogonHint()] if t)
                log.append({"kind": "login", "text": text or "ODIS online sign-in"})
                try:
                    creds = _job_ask(jid, {"kind": "login", "text": text or "Sign in to the VW online service (ODIS asks for it).",
                                           "error": error_text(failure) if failure is not None else None,
                                           "user": request.getUserId() or ""})
                except Exception:
                    creds = None
                if not creds or not creds.get("username") or not creds.get("password"):
                    return response(True, None, None)
                return response(False, creds["username"], creds["password"])

        props = Hashtable()
        props.put("service.ranking", Integer(1000))
        _KEEP["page_login"] = context.registerService(LOGIN_HANDLER, PageLogin(), props)
        return _KEEP["page_login"]
    except (Exception, Throwable) as e:
        log.append({"kind": "note", "text": "sign-in stays in ODIS's own window (%s)" % error_text(e)})
        return None


GRP_LOGON = "de.volkswagen.odis.vaudas.infoservices.grp.IGRPLogonService"
GRP_LOGIN_HANDLER = "de.volkswagen.odis.vaudas.infoservices.grp.IGRPLoginHandlerService"
GRP_COMM = "de.volkswagen.odis.vaudas.infoservices.IGRPCommunicationService"
GRP_TOKEN = "de.volkswagen.odis.vaudas.infoservices.grp.IGrpTokenProvider"

# Laertes's online stub (laertes.exe -grpstub) serves VW's GSB web service here. When it is running,
# online guided functions (e.g. component protection) are pointed at it so they complete without
# VW's backend; the virtual VCI accepts whatever the function then writes to the units.
ONLINE_STUB_HOST, ONLINE_STUB_PORT = "127.0.0.1", 23032
ONLINE_STUB_BASE = "http://%s:%d" % (ONLINE_STUB_HOST, ONLINE_STUB_PORT)
ONLINE_STUB_GSB = ONLINE_STUB_BASE + "/services"  # VW GSB web service (component protection, etc.)


def _online_stub_running():
    """Whether Laertes's online stub is reachable (so ODIS's online calls should go to it)."""
    import socket
    try:
        c = socket.create_connection((ONLINE_STUB_HOST, ONLINE_STUB_PORT), 0.3)
        c.close()
        return True
    except (Exception, Throwable):
        return False


def _reconfigure(obj, overrides):
    """Re-run an ODIS managed service's updated(Dictionary) with a copy of its current config
    (its private `dict` field) plus `overrides` {key: value}, for keys it already has. Returns True
    if anything changed. Used to point config URLs at Laertes's online stub at run time."""
    from java.lang import Class
    from java.util import Hashtable
    fdict = Class.getDeclaredField(obj.getClass(), "dict")
    fdict.setAccessible(True)
    cur = fdict.get(obj)
    new = Hashtable()
    if cur is not None:
        keys = cur.keys()
        while keys.hasMoreElements():
            k = keys.nextElement()
            new.put(k, cur.get(k))
    changed = False
    for k in list(new.keySet()):
        ks = unicode(k)
        for suffix, value in overrides:
            if ks.endswith(suffix) and new.get(k) != value:
                new.put(k, value)
                changed = True
    if changed:
        obj.updated(new)
    return changed


def _point_online_to_stub(context):
    """Point ODIS's GSB/secureID/PKI web-service URLs (TeTesterUrlService) at Laertes's online stub,
    so online guided functions (e.g. component protection) run offline. Done just before an online
    function runs (ODIS may reload its own config) and only when the stub is reachable, so running
    against VW's real backend (laertes -grpstub=false) is unaffected.

    The GRP sign-in (grp.discovery) is deliberately left alone: component protection's GSB call
    carries an empty security token, so no sign-in happens; repointing grp.discovery instead makes
    ODIS attempt the interactive OAuth token flow, which blocks a headless run. The stub still serves
    sign-in on the same port for anyone who points grp.discovery at it."""
    if not _online_stub_running():
        return False
    ref_srv, ref = gfs_service(context, "de.volkswagen.odis.vaudas.infoservices.IUrlReferenceService")
    if ref_srv is None:
        return False
    try:
        from java.lang import Class
        fsrv = Class.getDeclaredField(ref_srv.getClass(), "srv")
        fsrv.setAccessible(True)
        _reconfigure(fsrv.get(ref_srv), [(".gsb", ONLINE_STUB_GSB), (".secureID", ONLINE_STUB_GSB), (".pki", ONLINE_STUB_GSB)])
        return True
    finally:
        if ref is not None:
            context.ungetService(ref)


def _grp_discovery(context):
    """The GRP OpenID discovery URL ODIS is configured with ("grp.discovery" of its logon
    configuration, behind the authentication method service), or None."""
    auth, ref = gfs_service(context, "de.volkswagen.odis.vaudas.infoservices.IAuthenticationMethodService")
    try:
        logon = read_field(auth, "grp")
        url = logon.getGrpDiscovery() or read_field(logon, "dict").get("grp.discovery")
        return unicode(url) if url else None
    except (Exception, Throwable):
        return None
    finally:
        if ref is not None:
            context.ungetService(ref)


def _register_grp_login(context, browse, trace):
    """Registers the GRP sign-in ODIS looks for when it needs a token (IGRPLoginHandlerService;
    this ODIS has none, so its online login fails with "no login handler specified"). ODIS signs
    in to GRP by OAuth in a browser: the handler builds ODIS's login URL, browse(url, state) shows
    it to the user and returns the address the browser was sent back to
    (http://localhost/1?code=...&state=...), or None when the user gave up, and ODIS takes the
    result from there. trace(dict) records each step. Returns the registration to unregister."""
    from java.util import Hashtable
    from java.lang import Integer
    from org.python.core import Py
    import uuid
    grp, ref = gfs_service(context, GRP_LOGON)
    try:
        bundle = FrameworkUtil.getBundle(grp.getClass())
    finally:
        if ref is not None:
            context.ungetService(ref)
    grp_type = Py.java2py(bundle.loadClass(GRP_LOGIN_HANDLER))
    login_type, response = _login_types(context)

    class GrpLogin(grp_type, login_type):
        def setBrand(self, brand):
            trace({"step": "setBrand", "value": unicode(brand)})

        def setAdditionalBrands(self, brands):
            trace({"step": "setAdditionalBrands", "value": unicode(brands)})

        def setVin(self, vin):
            trace({"step": "setVin", "value": unicode(vin)})

        def updateLogoutButtonByGrpResult(self, ok):
            trace({"step": "updateLogoutButtonByGrpResult", "value": bool(ok)})

        def performLogin(self, request):
            try:
                comm, cref = gfs_service(context, GRP_COMM)
                try:
                    state = uuid.uuid4().hex
                    try:
                        url = comm.createLoginUrl(state, False, "")
                    except (Exception, Throwable):
                        # Nothing in this ODIS reads the discovery document before the first
                        # sign-in (its browser sign-in component would); read it, then retry.
                        discovery = _grp_discovery(context)
                        trace({"step": "initialize", "value": discovery})
                        comm.initialize(discovery)
                        url = comm.createLoginUrl(state, False, "")
                    trace({"step": "performLogin", "url": unicode(url)})
                    back = browse(unicode(url), state)
                    if not back:
                        trace({"step": "cancelled"})
                        return response(True, None, None)
                    accepted = comm.setBrowserResult(state, back)
                    trace({"step": "setBrowserResult", "value": bool(accepted)})
                    if not accepted:
                        return response(True, None, None)
                    comm.retrieveTokens(state)  # the code for ODIS's GRP tokens (ODIS does not)
                    trace({"step": "retrieveTokens", "user": unicode(comm.getUserId() or "")})
                    return response(False, comm.getUserId(), None)
                finally:
                    if cref is not None:
                        context.ungetService(cref)
            except (Exception, Throwable) as e:  # ODIS would swallow it; record it
                trace({"step": "error", "error": error_text(e)})
                return response(True, None, None)

    # ODIS finds its browser sign-in as a login handler with pwdType=browser
    # (DefaultLoginHandlerProvider's filter). Registered through the GRP bundle: registered from
    # ours, OSGi hides it from ODIS (our bundle is wired to other copies of these interfaces).
    props = Hashtable()
    props.put("pwdType", "browser")
    props.put("service.ranking", Integer(1000))
    return bundle.getBundleContext().registerService([LOGIN_HANDLER, GRP_LOGIN_HANDLER], GrpLogin(), props)


def _grp_sign_in(context, browse):
    """Asks ODIS for its GRP token the way an online function does (IGrpTokenProvider), with our
    GRP sign-in registered for the duration; ODIS signs in when it has no valid token. browse is
    as for _register_grp_login. Returns whether ODIS has a token afterwards, the GRP user, the
    steps taken and, when it has none, ODIS's reason."""
    steps = []
    registration = _register_grp_login(context, browse, steps.append)
    out = {"ok": True, "steps": steps}
    try:
        provider, pref = gfs_service(context, GRP_TOKEN)
        try:
            out["signed_in"] = bool(provider.getToken(None, True))
        except (Exception, Throwable) as e:
            out["signed_in"] = False
            out["error"] = error_text(e)
        finally:
            if pref is not None:
                context.ungetService(pref)
    finally:
        registration.unregister()
    failed = [x["error"] for x in steps if x["step"] == "error"]
    if failed:  # our part failed; that says more than ODIS's "no GRP Token available"
        out["error"] = failed[-1]
    out["cancelled"] = any(x["step"] == "cancelled" for x in steps)
    out["asked"] = any(x["step"] == "performLogin" for x in steps)  # False: a token was still valid
    comm, cref = gfs_service(context, GRP_COMM)
    try:
        out["user"] = unicode(comm.getUserId() or "")
    finally:
        if cref is not None:
            context.ungetService(cref)
    return out


def _page_browse(jid):
    """browse for _register_grp_login through the page: a job prompt "grp_browser" with the login
    URL and its state, answered with the address the browser was sent back to (null to give up)."""
    return lambda url, state: _job_ask(jid, {
        "kind": "grp_browser", "text": "Sign in to the VW online service in your browser.",
        "url": url, "state": state})


def handle_grp_login(cmd):
    """Signs ODIS in to the VW online service (GRP), as a job: the user signs in in their own
    browser (job prompt "grp_browser" with the login URL and its state; the answer is the address
    the browser was sent back to, null to give up). The job's result is _grp_sign_in's. Contacts
    VW's servers; touches nothing in the car."""
    context = get_context()

    def run(progress, jid):
        progress(0, "signing in to the VW online service", "sign-in")
        return _grp_sign_in(context, _page_browse(jid))
    return {"ok": True, "job_id": run_background(run, cmd, vehicle=False)}


def _stub_browse(url, state):
    """Development stand-in for the user's browser against Laertes's GRP stub (-grpstub): posts its dev
    account (admin/admin) to the login URL and returns where the stub redirects."""
    from java.net import URL
    from java.lang import String as JString
    conn = URL(url).openConnection()
    conn.setInstanceFollowRedirects(False)
    conn.setRequestMethod("POST")
    conn.setDoOutput(True)
    out = conn.getOutputStream()
    out.write(JString("username=admin&password=admin").getBytes("UTF-8"))
    out.close()
    conn.getResponseCode()
    return conn.getHeaderField("Location")


def handle__grp_login_stub(cmd):
    """Development: the GRP sign-in against Laertes's GRP stub (laertes.exe -grpstub) with its account,
    without a browser. Debug token required."""
    if cmd.get("token") != debug_token():
        return {"ok": False, "error": "missing or wrong debug token (see %s)" % DEBUG_TOKEN_FILE}
    return _grp_sign_in(get_context(), _stub_browse)


def _page_ui_connector(context, gfs, jid, progress, log, done):
    """An IGfsUiConnector that forwards the dialogs of an ODIS guided function to the page through
    the job's prompt channel, and its messages to the job. ODIS runs the function and decides what
    happens; the page only answers what ODIS asks."""
    from java.lang import Integer
    from java.util import List
    from org.python.core import Py
    bundle = FrameworkUtil.getBundle(gfs.getClass())
    pkg = "de.volkswagen.odis.vaudas.gfs.execution."
    ui_type = Py.java2py(bundle.loadClass(pkg + "IGfsUiConnector"))
    result = Py.java2py(bundle.loadClass(pkg + "UiInputResult"))
    action = Py.java2py(bundle.loadClass(pkg + "UiInputResult$EUserSpecialAction"))
    rc_type = Py.java2py(bundle.loadClass(pkg + "IRuntimeControl"))

    # ODIS drives cancellation and test-step synchronisation through this control. It must not be
    # null (the test program calls it directly); cancel is tied to the job so the page's Stop works.
    # The fields are underscore-named on purpose: a plain name like "cancelAllowed" would be a
    # JavaBean property of the interface, so assigning it would re-enter the setter (StackOverflow).
    class Runtime(rc_type):
        def __init__(self):
            self._sync = False
            self._cancel_allowed = True
            self._cancel_forbidden = False
            self._teststep_running = False
            self._cancelled = False

        def isTestProgramSynchronized(self):
            return self._sync

        def setTestProgramSynchronized(self, value):
            self._sync = bool(value)

        def isCancelAllowed(self):
            return self._cancel_allowed and not self._cancel_forbidden

        def setCancelAllowed(self, value):
            self._cancel_allowed = bool(value)

        def setCancelForbidden(self, value):
            self._cancel_forbidden = bool(value)

        def isCancelTeststepRunning(self):
            return self._teststep_running

        def setCancelTeststepRunning(self, value):
            self._teststep_running = bool(value)

        def requestCancel(self):
            self._cancelled = True

        def isCancelled(self):
            return self._cancelled or _job_cancelled(jid)

    runtime = Runtime()

    def text(t):
        try:
            return odis_text(context, t)
        except (Exception, Throwable):
            return unicode(t)

    def note(kind, msg):
        # "t" (Unix seconds) times each entry, so the page can show when it happened and match the
        # CAN traffic of that moment to the step that caused it.
        log.append({"kind": kind, "text": msg, "t": time.time()})
        _job_set(jid, message=msg, data={"log": log})

    def ask(prompt):
        try:
            return True, _job_ask(jid, prompt)
        except Exception:  # cancelled, or the page went away: answer ODIS with "cancel"
            return False, None

    def cancelled():
        return result(action.CANCEL, None)

    def answered(ok, what, choice=None):
        """Logs the page's answer to a dialog, so the run shows each step that was chosen. The
        chosen ODIS text is kept apart from our own words, so only it is translated."""
        entry = {"kind": "you", "text": what if ok else "cancelled", "t": time.time()}
        if ok and choice is not None:
            entry["choice"] = choice
        log.append(entry)
        _job_set(jid, message=entry["text"], data={"log": log})

    class Connector(ui_type):
        def getUserInput(self, title, options, *rest):
            ok, value = ask({"kind": "input", "text": text(title),
                             "initial": options.getInitialValue() if options is not None else None})
            answered(ok and value is not None, "entered: %s" % value)
            return result(unicode(value)) if ok and value is not None else cancelled()

        def getUserSelection(self, choices, other, *rest):
            items = [text(c) if (hasattr(c, "getSource") or hasattr(c, "getTextId")) else unicode(c) for c in (choices or other or [])]
            if len(items) == 1:  # a single "choice" is ODIS's way of showing a text to acknowledge
                note("message", items[0])
                ok, _ = ask({"kind": "message", "text": items[0]})
                answered(ok, "OK")
                return result(Integer(0)) if ok else cancelled()
            ok, value = ask({"kind": "select", "text": "ODIS asks you to choose", "options": items})
            if ok and value is not None and 0 <= int(value) < len(items):
                answered(True, "chose", items[int(value)])
            else:
                answered(False, "")
            return result(Integer(int(value))) if ok and value is not None else cancelled()

        def getUserDialogDecisionToContinue(self, title, question):
            ok, value = ask({"kind": "confirm", "text": " ".join(filter(None, [text(title), text(question)]))})
            answered(ok, "Continue" if value else "No")
            return bool(ok and value)

        def showMessage(self, msg, wait):
            body = "\n".join(text(m) for m in (msg if isinstance(msg, List) else [msg]))
            note("message", body)
            if wait:
                ok, _ = ask({"kind": "message", "text": body})
                answered(ok, "OK")

        def showErrorMessage(self, msg, error):
            note("error", " ".join(filter(None, [text(msg), error_text(error) if error is not None else None])))

        def showTestProgramTitle(self, title):
            note("title", text(title))

        def showProgress(self, title, *values):
            note("progress", text(title))

        def updateProgress(self, value):
            pass

        def hideProgess(self):
            pass

        def programStarted(self, handle, entry):
            progress(10, "ODIS is running the guided function", "run")

        def programFinished(self, handle):
            done.set()

        def programAborted(self, handle, error):
            note("error", "aborted: %s" % error_text(error))
            done.set()

        def stepStateChanged(self, step, event):
            pass

        def setTestNodeStack(self, stack):
            pass

        def getUserInputForPartNumberAndSerialNumber(self):
            return None

        def getUserInputForNonDiagnosticComponents(self):
            from java.util import ArrayList
            return ArrayList()

        def readMeasurementValues(self, *args):
            from java.util import ArrayList
            return ArrayList()

        def showMeasurementValueTableResults(self, results):
            pass

        def doGatewayOperation(self, description):
            description.setCanceled(True)
            return description

        def showHelp(self, *args):
            pass

        def showGlobalDocumentSelection(self, docs):
            pass

        def showLocalDocumentSelection(self, docs):
            pass

        def clearGlobalDocuments(self):
            pass

        def clearLocalDocuments(self):
            pass

        def getRuntimeControl(self):
            return runtime

        def isUiLocked(self):
            return False

        def lockUi(self, locked):
            pass

        def isVerificationMode(self, value):
            return False

        def clear(self):
            pass

        def dispose(self):
            pass

    return Connector()


def handle_gf_run(cmd):
    """Runs a guided function in ODIS, as a background job. ODIS executes it (it talks to the units
    and to VW's online service) and decides the outcome; the page sees its messages and answers its
    dialogs, including the online sign-in, through the job's prompt channel. The function is chosen
    by cmd["key"] (a diag-object key from gf_functions, for vehicle-wide functions such as the
    immobilizer) or, for component protection, by cmd["unit"] (a unit address, e.g. "5F"). Needs the
    vehicle identified (gf_identify) and the car connected."""
    key, unit = cmd.get("key"), cmd.get("unit") or ""
    if not key and not unit:
        return {"ok": False, "error": "missing key (a function from gf_functions) or unit (an address, e.g. 5F)"}
    try:
        context, gfs, kb, vehicle = _gfs_build(cmd)
    except (Exception, Throwable) as e:
        return {"ok": False, "error": error_text(e)}
    # If Laertes's online stub is running, route ODIS's online calls (e.g. component protection) to
    # it so the function can complete without VW's backend. No effect otherwise.
    try:
        _point_online_to_stub(context)
    except (Exception, Throwable):
        pass
    if key:
        obj, handle = _find_function_by_key(kb, key)
        missing = "ODIS has no guided function with key %s for this vehicle" % key
    else:
        obj, handle = _find_cp_function(kb, unit)
        missing = "ODIS has no component-protection guided function for unit %s" % unit
    if obj is None or handle is None:
        return {"ok": False, "error": missing}
    name = odis_text(context, obj.getName())

    def job(progress, jid):
        from org.python.core import Py
        log, done = [], threading.Event()
        progress(5, "Starting %s in ODIS" % name, "prepare")
        bundle = FrameworkUtil.getBundle(gfs.getClass())
        origin = Py.java2py(bundle.loadClass("de.volkswagen.odis.vaudas.gfs.execution.ExecutionOrigin"))
        start = [m for m in gfs.getClass().getMethods() if m.getName() == "startTestProgram"][0]
        error_type = Py.java2py(start.getParameterTypes()[4])
        registration = _register_page_login(context, jid, log)
        # ODIS's online functions (e.g. component protection) sign in to GRP in the browser.
        grp_registration = _register_grp_login(context, _page_browse(jid), lambda step: step["step"] == "error" and log.append(
            {"kind": "error", "text": "VW online sign-in: %s" % step["error"]}))
        try:
            run = gfs.startTestProgram(handle, obj, _page_ui_connector(context, gfs, jid, progress, log, done),
                                       False, error_type.UNDEFINED, origin.GuidedFunction)
            # startTestProgram may return before the program ends; then wait for ODIS to report it.
            while not done.is_set() and (run is None or run.getResult() is None):
                if _job_cancelled(jid):
                    raise Exception("cancelled")
                done.wait(0.5)
        finally:
            grp_registration.unregister()
            if registration is not None:
                registration.unregister()
        outcome = run.getResult() if run is not None else None
        progress(100, "ODIS finished %s" % name, "done")
        return {"function": name, "gfsid": unicode(obj.getDiagObjectGfsId()), "unit": unit,
                "outcome": unicode(outcome) if outcome is not None else None, "log": log}

    return {"ok": True, "job_id": run_background(job, cmd), "function": name}


# Car backup: ODIS-E's Vehicle functions › 046 Vehicle special functions › 046.01 Coding /
# Adaptations, "Read data". ODIS's own EcuDataTransferResultModel reads the coding and adaptation
# channels of every unit and writes its result protocol (Cod_Anp_<VIN>_<time>.xml, plus the .html
# view ODIS renders from it), the file the same function can later write back to the units. Its UI
# action only adds a file dialog and a progress dialog, so the model is driven directly.

def _bundle(name):
    """The OSGi bundle with symbolic name `name`, to load ODIS classes no service hands out."""
    for b in get_context().getBundles():
        if b.getSymbolicName() == name:
            return b
    raise Exception("ODIS has no bundle %s" % name)


def handle_car_backup(cmd):
    """Reads coding and adaptations of the units at cmd["addresses"] (unit addresses as numbers;
    without them, the units ODIS itself lists for the function) into a new result protocol in
    cmd["dir"], as a background job. cmd["coding"], cmd["adaptation"] and cmd["kwp"] (read KWP units
    too) default to true, as the checkboxes of 046.01 are ticked for a full backup. Only reads."""
    from java.lang import Class
    from java.util import ArrayList
    from org.eclipse.core.runtime import NullProgressMonitor
    out_dir = cmd.get("dir") or ""
    if not out_dir:
        return {"ok": False, "error": "missing dir (the folder for the backup file)"}
    business = _bundle("de.volkswagen.odis.vaudas.business")
    unit_class = business.loadClass("de.volkswagen.odis.vaudas.business.zecret.UiControlUnitImpl")
    wrap = [c for c in Class.getConstructors(unit_class) if len(c.getParameterTypes()) == 1][0]
    ecus = list(session_vehicle().getActualECUs() or [])
    if not ecus:
        return {"ok": False, "error": "ODIS has no units for this vehicle; load the vehicle project first"}
    units = [wrap.newInstance([e]) for e in ecus]
    model_class = _bundle("de.volkswagen.odis.vaudas.business.vehiclefunction").loadClass(
        "de.volkswagen.odis.vaudas.business.vehiclefunction.zecret.ecudatatransfer.EcuDataTransferResultModel")
    empty = Class.getField(business.loadClass("de.volkswagen.odis.vaudas.i18n.Messages"), "EMPTY_TEXT_ID").get(None)
    # The model belongs to a unit, as every self-diagnosis function does; the gateway's, as in ODIS.
    gateway = ([u for u in units if u.getAddress() == 0x19] or units)[0]
    model = Class.getConstructors(model_class)[0].newInstance([gateway, empty, empty])
    model.setActive(True)
    if cmd.get("addresses"):
        want = set(int(a) for a in cmd["addresses"])
        chosen = ArrayList()
        for u in units:
            if u.getAddress() in want:
                chosen.add(u)
        model.setVisibleControlUnits(chosen)
    chosen = list(model.getVisibleControlUnits() or [])
    if not chosen:
        model.setActive(False)
        return {"ok": False, "error": "no units to back up: none of the addresses is a unit of the vehicle project"}
    if cmd.get("coding", True):
        model.selectCodingForAll()
    if cmd.get("adaptation", True):
        model.selectAdaptationForAll()
    model.setSelectKwp(bool(cmd.get("kwp", True)))
    if not model.isReadDataOfControlUnitsPossible():
        model.setActive(False)
        return {"ok": False, "error": "ODIS cannot read the backup now (choose coding, adaptations or both)"}
    vin = unicode(model.getVIN() or "").strip()
    path = os.path.join(out_dir, "Cod_Anp_%s%s.xml" % (vin + "_" if vin else "", time.strftime("%Y%m%dT%H%M%S")))
    model.setProtFilePath(path)

    def job(progress, jid):
        # ODIS reports each unit through the monitor; a cancelled job stops it between units. The
        # fields are underscore-named so they do not hide the monitor's own methods (done()).
        class Monitor(NullProgressMonitor):
            _total, _worked = 0, 0

            def beginTask(self, name, total):
                self._total, self._worked = total, 0
                progress(1, unicode(name or "Reading the units"), "reading")

            def subTask(self, name):
                if name:
                    progress(None, unicode(name))

            def setTaskName(self, name):
                self.subTask(name)

            def worked(self, n):
                self._worked += n
                if self._total > 0:
                    progress(min(99, int(100.0 * self._worked / self._total)))

            def isCanceled(self):
                return _job_cancelled(jid)

        progress(0, "Reading coding and adaptations of %d units" % len(chosen), "reading")
        try:
            model.readEcuTransferData(Monitor())
        finally:
            model.setActive(False)
        if _job_cancelled(jid):
            raise Exception("cancelled")
        if not os.path.exists(path):
            raise Exception("ODIS wrote no backup file (see its engine.log)")
        html = path[:-4] + ".html"
        progress(100, "Backup saved", "done")
        return {"xml": path, "html": html if os.path.exists(html) else None, "vin": vin, "units": len(chosen)}

    return {"ok": True, "job_id": run_background(job, cmd), "file": path, "units": len(chosen)}


def handle_project_state(cmd):
    """Read-only: the current/available vehicle project, to design offline simulation."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    out = {"ok": True}
    diag = Factory.getInstance()
    for name, fn in [("vehicle_project", diag.getVehicleProject),
                     ("change_allowed", diag.isVehicleProjectChangeAllowed),
                     ("project_mode", diag.getAutomationApiVersion)]:
        try:
            out[name] = fn()
        except (Exception, Throwable) as e:
            out[name + "_error"] = error_text(e)
    try:
        out["project_ecus"] = [unicode(x) for x in diag.getProjectEcuAddressList()]
    except (Exception, Throwable) as e:
        out["project_ecus_error"] = error_text(e)
    context = get_context()
    info, ref = gfs_service(context, "de.volkswagen.odis.vaudas.diag.systemtest.IDiagnosisSessionInfoService")
    try:
        if info is not None:
            out["proposed_manufacturers"] = [unicode(x) for x in info.getProposedManufacturerList()]
            out["proposed_projects"] = [unicode(x) for x in info.getProposedVehicleProjectList()]
    except (Exception, Throwable) as e:
        out["proposed_error"] = error_text(e)
    finally:
        if ref is not None:
            context.ungetService(ref)
    return out


def handle_api_methods(cmd):
    """Read-only: the methods of the ODIS automation diagnostic interface (IDiagnosticInterface),
    to see which read/write operations (coding, adaptation) are available over the bridge."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    diag = Factory.getInstance()
    return {"ok": True, "class": diag.getClass().getName(), "methods": gfs_methods(diag)}


def handle_vehicle(cmd):
    """Just the current vehicle project, for a lightweight top-bar status poll."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    try:
        return {"ok": True, "project": Factory.getInstance().getVehicleProject()}
    except (Exception, Throwable):
        return {"ok": True, "project": None}


def handle_vin(cmd):
    """Best-effort vehicle VIN from the current ODIS session; empty if ODIS does not know it."""
    out = {"ok": True, "vin": None}
    context = get_context()
    info, ref = gfs_service(context, "de.volkswagen.odis.vaudas.diag.systemtest.IDiagnosisSessionInfoService")
    try:
        for getter in ("getVehicleIdentificatonData", "getIdleVin"):
            try:
                obj = getattr(info, getter)()
            except (Exception, Throwable):
                continue
            if obj is None:
                continue
            for m in ("getVin", "getVIN", "getFin", "getVehicleIdentificationNumber", "getIdentNumber", "toString"):
                try:
                    v = getattr(obj, m)()
                except (Exception, Throwable):
                    continue
                if v is None:
                    continue
                mo = re.search(r"[A-HJ-NPR-Z0-9]{17}", unicode(v))  # a VIN is 17 chars, no I/O/Q
                if mo:
                    out["vin"] = mo.group(0)
                    return out
    finally:
        if ref is not None:
            context.ungetService(ref)
    return out


def handle_vci(cmd):
    """The vehicle communication interface (VCI) state/type, for the top-bar status."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    diag = Factory.getInstance()
    out = {"ok": True}
    for k, fn in (("state", diag.checkVciState), ("clamp15", diag.getClamp15State), ("clamp30", diag.getClamp30State)):
        try:
            out[k] = unicode(fn())
        except (Exception, Throwable):
            pass
    try:
        out["doip"] = [unicode(x) for x in (diag.getDoIPVCIs() or [])]
    except (Exception, Throwable):
        pass
    return out


def handle_reset_vci(cmd):
    """Restarts the vehicle communication interface (VCI), then reports its state. Waits for any
    running diagnostic job, since a reset in the middle of one would break it."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory

    def reset():
        diag = Factory.getInstance()
        diag.resetVCI()
        out = {"ok": True, "reset": True}
        try:
            out["state"] = unicode(diag.checkVciState())
        except (Exception, Throwable) as e:
            out["state_error"] = error_text(e)
        return out
    return with_database(reset)


def handle_doip_activation(cmd):
    """Sets the DoIP activation line on or off (the DoIP wake-up for Ethernet-diagnosed cars)."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    on = bool(cmd.get("on", True))

    def run():
        Factory.getInstance().setDoIPActivationLine(on)
        return {"ok": True, "doip_activation": on}
    return with_database(run)


def handle_vehicle_projects(cmd):
    """The current vehicle project and whether it can be changed, plus any selectable projects
    ODIS proposes. Used to let the user pick the car for offline/guided data when not detected."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    diag = Factory.getInstance()
    out = {"ok": True}
    for name, fn in [("current", diag.getVehicleProject), ("change_allowed", diag.isVehicleProjectChangeAllowed)]:
        try:
            out[name] = fn()
        except (Exception, Throwable) as e:
            out[name + "_error"] = error_text(e)
    for name, fn in [("vehicle_infos", lambda: diag.getVehicleInfos("")), ("odx_versions", diag.getODXVersions)]:
        try:
            out[name] = [unicode(x) for x in (fn() or [])]
        except (Exception, Throwable) as e:
            out[name + "_error"] = error_text(e)
    context = get_context()
    info, ref = gfs_service(context, "de.volkswagen.odis.vaudas.diag.systemtest.IDiagnosisSessionInfoService")
    try:
        if info is not None:
            out["proposed_projects"] = [unicode(x) for x in (info.getProposedVehicleProjectList() or [])]
            out["proposed_manufacturers"] = [unicode(x) for x in (info.getProposedManufacturerList() or [])]
    except (Exception, Throwable) as e:
        out["proposed_error"] = error_text(e)
    finally:
        if ref is not None:
            context.ungetService(ref)
    return out


def handle_lookup_projects(cmd):
    """All vehicle projects ODIS can offer (for choosing the car from the web page)."""
    context = get_context()
    svc, ref = gfs_service(context, "de.volkswagen.odis.vaudas.diag.vehicledata.IVaudasVehicleService")
    try:
        if svc is None:
            return {"ok": False, "error": "vehicle service not found"}
        return {"ok": True, "projects": sorted(set(unicode(x) for x in (svc.lookupProjects() or [])))}
    except (Exception, Throwable) as e:
        return {"ok": False, "error": error_text(e)}
    finally:
        if ref is not None:
            context.ungetService(ref)


def handle_set_vehicle_project(cmd):
    """Sets the vehicle project (the car) so its ODX data loads when ODIS did not detect one."""
    from de.volkswagen.odis.vaudas.vehiclefunction.automation.IDiagnosticInterface import Factory
    project = cmd.get("project")
    if not project:
        return {"ok": False, "error": "missing project"}
    diag = Factory.getInstance()
    try:
        result = diag.setVehicleProject(project)
    except (Exception, Throwable) as e:
        return {"ok": False, "error": error_text(e)}
    ecus = []
    try:
        ecus = [unicode(x) for x in (diag.getProjectEcuAddressList() or [])]
    except (Exception, Throwable):
        pass
    return {"ok": True, "project": unicode(result), "ecu_count": len(ecus)}


def _didb_connection(adapter, locale):
    """The JDBC connection of an ODIS database adapter (AdapterRuntime, a non-public class, so its
    getConnection(Locale) is called by reflection) for the texts of `locale`. It is ODIS's own
    shared, cached connection: never close it (close only the statements and result sets)."""
    from java.lang import Class
    from java.util import Locale
    m = Class.getMethod(adapter.getClass(), "getConnection", [Locale])
    m.setAccessible(True)
    return m.invoke(adapter, [locale])


def didb_texts(keys, locale=None, source=None):
    """{key: text} for the text keys (e.g. "GUID6828D7...", "displayName080044...") found in ODIS's
    databases (Base and the brand's GFS texts, table TRANSLATEDTEXT) in `locale` (default the ODIS
    language). Read-only; keys found nowhere are left out. A key such as "TXDEF3" means a different
    text in each test program, so `source` (its TEXTSOURCE, e.g. "080044de8000cee6") picks one."""
    from java.util import Locale
    keys = [k for k in set(keys or []) if k]
    found = {}
    if not keys:
        return found
    context = get_context()
    mgr, ref = gfs_service(context, "de.volkswagen.odis.vaudas.didb.adapter.IOdisAdapterManagerService")
    try:
        for adapter in mgr.getAdapters() or []:
            try:
                conn = _didb_connection(adapter, locale or Locale.getDefault())
                st = conn.prepareStatement("SELECT TEXTID, TEXT FROM TRANSLATEDTEXT WHERE TEXTID IN (%s)%s"
                                           % (",".join("?" * len(keys)), " AND TEXTSOURCE = ?" if source else ""))
                for i, k in enumerate(keys):
                    st.setString(i + 1, k)
                if source:
                    st.setString(len(keys) + 1, source)
                rs = st.executeQuery()
                while rs.next():
                    found.setdefault(unicode(rs.getString(1)), unicode(rs.getString(2)))
                rs.close()
                st.close()
            except (Exception, Throwable):
                pass  # an adapter without this language/brand database
    finally:
        if ref is not None:
            context.ungetService(ref)
    return found


_GFS_TEXTS = {}


def _xml_text(text):
    """Plain text of an ODIS XML text (<String>…</String>): <BR/> becomes a line break, the rest of
    the markup is removed. A test-program variable (<Var name="str_wfs_zustand"/>) becomes
    ‹str_wfs_zustand›: odis_text fills in its value, else it shows where the value belongs."""
    text = re.sub(r"<BR\s*/>", "\n", re.sub(r"<\?xml[^>]*\?>", "", text))
    text = re.sub(r"<Var\s+name=\"([^\"]*)\"\s*/>", u"‹\\1›", text)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text.strip()


def gfs_db_text(key, source=None):
    """Plain text of an ODIS database text key (guided-function texts such as "GUID6828D7..." or
    "displayName080044..."), or None. Tried in the ODIS language first, then German: ODIS ships the
    guided-function (GFS) texts only in German. Their XML markup (<BR/>, <Format>) is removed.
    `source` is the key's text source (see didb_texts), when known."""
    from java.util import Locale
    cached = (source, key)
    if cached not in _GFS_TEXTS:  # only found texts are kept, so a failed lookup is retried
        text = didb_texts([key], None, source).get(key) or didb_texts([key], Locale.GERMANY, source).get(key)
        if text and text.lstrip().startswith("<"):
            text = _xml_text(text)
        if not text:
            return None
        _GFS_TEXTS[cached] = text
    return _GFS_TEXTS[cached]


def handle_text_lookup(cmd):
    """Read-only: the texts of the keys in cmd["keys"] from ODIS's databases, in cmd["locale"]
    (a language tag such as "en-GB"; default the ODIS language), and each key's plain text as the
    guided functions show it (German fallback, markup removed)."""
    from java.util import Locale
    locale = Locale.forLanguageTag(cmd["locale"]) if cmd.get("locale") else None
    out = {"ok": True, "texts": didb_texts(cmd.get("keys"), locale), "plain": {}}
    for k in cmd.get("keys") or []:
        try:
            out["plain"][k] = gfs_db_text(k)
        except (Exception, Throwable) as e:
            out["plain"][k] = "ERROR " + error_text(e)
    return out


def handle_list_services(cmd):
    """Read-only: registered OSGi service class names containing cmd['q'] (for exploration)."""
    q = (cmd.get("q") or "").lower()
    context = get_context()
    names = set()
    for r in context.getAllServiceReferences(None, None) or []:
        for n in (r.getProperty("objectClass") or []):
            if q in n.lower():
                names.add(n)
    return {"ok": True, "services": sorted(names)}


def handle_service_methods(cmd):
    """Read-only: the methods of any registered OSGi service, for exploration."""
    context = get_context()
    svc, ref = gfs_service(context, cmd.get("service"))
    try:
        if svc is None:
            return {"ok": False, "error": "service not found"}
        return {"ok": True, "class": svc.getClass().getName(), "methods": gfs_methods(svc)}
    finally:
        if ref is not None:
            context.ungetService(ref)


# --- Long-running jobs with progress (flash firmware / programming configuration / data transfer) -----------------------
# These operations take minutes, so they run in a background thread and the web page polls
# "job_status" for progress (percent, the text ODIS reports, and a phase). One runs at a time on
# the vehicle (DIAG_LOCK). Flashing is driven through the ODIS programming API
# (IProgrammingConfigReader + IProgrammingConfigExecutor). EXPERIMENTAL / UNTESTED: a wrong or
# interrupted flash can leave a unit inoperable; recovery may need an online VW/ODIS SVM session.

# Kept in the reload-surviving store (_KEEP) so a handler reload does not orphan jobs in flight.
_JOBS = _KEEP.setdefault("jobs", {})
_job_seq = _KEEP.setdefault("job_seq", [0])  # job ids stay unique across reloads
_JOBS_LOCK = threading.Lock()


def _new_job(resume=None):
    with _JOBS_LOCK:
        _job_seq[0] += 1
        jid = "job%d" % _job_seq[0]
        _JOBS[jid] = {"status": "running", "percent": 0, "message": "starting", "phase": "prepare",
                      "result": None, "error": None, "cancel": False, "data": None,
                      "polled": time.time(), "seq": _job_seq[0], "resume": resume}
    return jid


def _job_set(jid, percent=None, message=None, phase=None, data=None):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return
        if data is not None:
            j["data"] = data
        if percent is not None:
            j["percent"] = percent
        if message is not None:
            j["message"] = message
        if phase is not None:
            j["phase"] = phase


def _job_cancelled(jid):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return bool(j and j["cancel"])


def _job_unpolled_for(jid):
    """Seconds since the web page last asked for the job's status."""
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return time.time() - j["polled"] if j else 0


def _job_finish(jid, result=None, error=None):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return
        j["status"] = "error" if error else "done"
        j["finished"] = time.time()
        j["error"] = error
        j["result"] = result
        if not error:
            j["percent"] = 100


def _job_ask(jid, prompt):
    """Shows prompt to the page and waits for its answer (job_answer), which it returns. A prompt
    is a dict with "kind" (login, input, select, confirm), "text" and, for select, "options".
    job_status carries the open prompt. Raises when the job is cancelled or the page has stopped
    polling, so a closed page does not leave the job waiting. Answers are not kept afterwards."""
    with _JOBS_LOCK:
        j = _JOBS[jid]
        j["prompt_seq"] = j.get("prompt_seq", 0) + 1
        j["prompt"], j["answer"] = dict(prompt, id=j["prompt_seq"]), None
    try:
        while True:
            with _JOBS_LOCK:
                if j["answer"] is not None:
                    return j["answer"]["value"]
            if _job_cancelled(jid):
                raise Exception("cancelled")
            # Long enough to close and reopen (or reload) the page and answer it there.
            if _job_unpolled_for(jid) > 300:
                raise Exception("the page stopped following the job")
            time.sleep(0.2)
    finally:
        with _JOBS_LOCK:
            j["prompt"], j["answer"] = None, None


def handle_job_answer(cmd):
    """Answers a job's open prompt: "prompt_id" (from job_status) and "value"."""
    jid = cmd.get("job_id")
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return {"ok": False, "error": "no such job: %s" % jid}
        prompt = j.get("prompt")
        if prompt is None or prompt["id"] != cmd.get("prompt_id"):
            return {"ok": False, "error": "the job is not waiting for this answer"}
        j["answer"] = {"value": cmd.get("value")}
        return {"ok": True, "job_id": jid}


def run_background(fn, cmd=None, vehicle=True):
    """Starts fn(progress, job_id) in a daemon thread and returns the job id. progress(percent,
    message, phase) updates it. A vehicle job holds DIAG_LOCK for its lifetime, so no other vehicle
    operation overlaps; vehicle=False for one that does not touch the car. cmd["resume"] (what the
    page needs to show the job again after a reload, opaque here) is kept with the job and listed
    by job_list."""
    jid = _new_job((cmd or {}).get("resume"))

    def worker():
        progress = lambda p, m=None, ph=None: _job_set(jid, p, m, ph)
        if vehicle:
            DIAG_LOCK.acquire()
        try:
            _job_finish(jid, result=fn(progress, jid))
        except (Exception, Throwable) as e:
            traceback.print_exc()
            _job_finish(jid, error=error_text(e))
        finally:
            if vehicle:
                DIAG_LOCK.release()

    t = threading.Thread(target=worker)
    t.setDaemon(True)
    t.start()
    return jid


def handle_job_status(cmd):
    """Progress of a background job (flash_run, basic_setting_run, output_test_run): percent, the latest text from
    ODIS, phase, state and the job's latest data (a basic setting's status and values)."""
    jid = cmd.get("job_id")
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return {"ok": False, "error": "no such job: %s" % jid}
        j["polled"] = time.time()
        out = {"ok": True, "job_id": jid, "status": j["status"], "percent": j["percent"],
               "message": j["message"], "phase": j["phase"], "cancel_requested": j["cancel"],
               "data": j["data"], "prompt": j.get("prompt")}
        if j["status"] == "done":
            out["result"] = j["result"]
        if j["status"] == "error":
            out["job_error"] = j["error"]
        return out


def handle_threads(cmd):
    """Diagnostics, read-only: the state and stack of ODIS's threads, all of them or those whose name
    or stack contains cmd["match"] (case-insensitive), at most cmd["depth"] frames each. Shows what
    a guided function or job is waiting for while it seems to hang."""
    from java.lang import Thread
    match = (cmd.get("match") or "").lower()
    depth = int(cmd.get("depth", 40))
    out = []
    for e in Thread.getAllStackTraces().entrySet():
        t = e.getKey()
        stack = [unicode(f) for f in e.getValue()]
        if match and match not in (unicode(t.getName()) + "\n" + "\n".join(stack)).lower():
            continue
        out.append({"name": unicode(t.getName()), "state": unicode(t.getState()), "stack": stack[:depth]})
    out.sort(key=lambda t: t["name"])
    return {"ok": True, "threads": out}


def handle_job_list(cmd):
    """The jobs a reopened page can follow again: those still running and those that finished
    after the page last polled them (oldest first), each with its id, status and resume data."""
    with _JOBS_LOCK:
        jobs = [(j["seq"], jid, j) for jid, j in _JOBS.items()
                if j["status"] == "running" or j.get("finished", 0) > j["polled"]]
        return {"ok": True, "jobs": [{"job_id": jid, "status": j["status"], "resume": j.get("resume")}
                                     for _, jid, j in sorted(jobs)]}


def handle_job_cancel(cmd):
    """Requests cancellation of a background job. A basic setting is stopped at its next check.
    A flash honors it only before the actual programming starts; once the unit is being written,
    ODIS's executor cannot be interrupted safely, so the flag is recorded but the flash runs to
    completion."""
    jid = cmd.get("job_id")
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None:
            return {"ok": False, "error": "no such job: %s" % jid}
        j["cancel"] = True
        return {"ok": True, "job_id": jid, "phase": j["phase"]}


FLASH_READER = "de.volkswagen.odis.vaudas.diag.flash.IProgrammingConfigReader"
FLASH_EXECUTOR = "de.volkswagen.odis.vaudas.diag.flash.IProgrammingConfigExecutor"
FLASH_FACTORY = "de.volkswagen.odis.vaudas.diag.flash.IProgrammingConfigFactory"


def _read_programming_config(context, path, cmd):
    """The programming configuration to execute. An XML programming configuration file is read
    as is; a flash container (.frf/.odx) is wrapped into a configuration with one FLASH-ECU step
    on the logical link of the unit at cmd["address"]."""
    from java.io import File
    f = File(path)
    if not f.exists():
        raise Exception("file not found on the ODIS PC: %s" % path)
    if path.lower().endswith((".frf", ".odx")):
        if not cmd.get("address"):
            raise Exception("flashing a container file needs the unit address")
        link = odx_link(cmd).getShortName()
        factory, ref = gfs_service(context, FLASH_FACTORY)
        try:
            if factory is None:
                raise Exception("IProgrammingConfigFactory not available")
            content = factory.createProgrammingConfigContent()
            # The flash sequencer trims the session name, so "no session" must be "" rather than null.
            content.addProgrammingStep(factory.createFlashEcuProgrammingStep(link, f.getAbsolutePath(), ""))
            return content
        finally:
            if ref is not None:
                context.ungetService(ref)
    reader, ref = gfs_service(context, FLASH_READER)
    try:
        if reader is None:
            raise Exception("IProgrammingConfigReader not available")
        return reader.read(f)
    finally:
        if ref is not None:
            context.ungetService(ref)


def handle_flash_config(cmd):
    """Reads and validates an ODIS programming configuration file (read-only, nothing is written):
    returns its diagnostic system and programming steps so the flash can be checked before running."""
    path = cmd.get("file") or cmd.get("path")
    if not path:
        return {"ok": False, "error": "flash_config needs a file"}

    def read():
        content = _read_programming_config(get_context(), path, cmd)
        return {"ok": True, "file": path, "content": to_data(content)}
    return with_database(read)


def handle_flash_run(cmd):
    """EXPERIMENTAL / UNTESTED. Flashes the programming configuration file "file" to the current
    vehicle through IProgrammingConfigExecutor.execute, reporting ODIS's progress text. The last
    argument of execute (an int) is not documented; it defaults to 1 and can be set with
    "repetitions". A wrong or interrupted flash can leave a unit inoperable."""
    path = cmd.get("file") or cmd.get("path")
    if not path:
        return {"ok": False, "error": "flash_run needs a file"}
    reps = int(cmd.get("repetitions", 1))

    def job(progress, jid):
        from java.io import File
        from de.volkswagen.odis.vaudas.core.event import CallbackListener
        context = get_context()
        progress(3, "Reading programming configuration...", "prepare")
        if _job_cancelled(jid):
            return {"cancelled": True, "stage": "before reading"}
        content = _read_programming_config(context, path, cmd)
        if _job_cancelled(jid):
            return {"cancelled": True, "stage": "after reading, before flashing"}
        vehicle = session_vehicle()
        executor, ref = gfs_service(context, FLASH_EXECUTOR)
        try:
            if executor is None:
                raise Exception("IProgrammingConfigExecutor not available")
            state = {"pct": 10}

            class _Listener(CallbackListener):
                def callbackPerformed(self, event):
                    try:
                        uo = event.getUserObject()
                        if hasattr(uo, "getCurrentAction"):  # IProgrammingProgressInfo
                            text = unicode(uo.getCurrentAction() or "flashing...")[:300]
                            state["pct"] = max(state["pct"], min(97, 10 + int(uo.getTotalProgress() * 0.87)))
                        else:
                            text = unicode(uo)[:300] if uo is not None else "flashing..."
                            state["pct"] = min(97, state["pct"] + 2)
                        progress(state["pct"], text, "flashing")
                    except (Exception, Throwable):
                        pass

                def handleException(self, e):
                    progress(None, "ODIS reported: %s" % error_text(e), "flashing")

            progress(10, "Starting programming. Do not switch off or disconnect.", "flashing")
            result = executor.execute(vehicle, content, _Listener(), reps)
            # ODIS skips a step whose programming preconditions the unit does not meet (e.g. the
            # immobiliser has not released it) and still reports the sequence as finished.
            skipped = [l.split(": ", 1)[-1] for l in _flash_log_lines(log_offset)[0]
                       if "Programming Precondition:" in l or "the flashing step is disabled" in l]
            if skipped:
                raise Exception("ODIS did not flash the unit: " + "; ".join(skipped))
            out = {"executed": True, "result": to_data(result)}
            if _job_cancelled(jid):
                out["cancel_requested"] = True
            return out
        finally:
            if ref is not None:
                context.ungetService(ref)
    log_offset = _engine_log_size()  # read by job() once execute returns
    return {"ok": True, "job_id": run_background(job, cmd), "log_offset": log_offset}


# ODIS protocols of flashes and data transfers, written the way ODIS's own screens write them.
# The automation interface keeps a protocol (IDiagnosticsProtocolHandler over ODIS's
# ISelfDiagProtocolModel): started before a job, it records what the job does (dataSetDownload adds
# its "010.01 Writing the Data Set" result, a failed command its error), and storeProtocol writes the
# .xml, the .html ODIS renders from it with ODIS_E_PROTOCOL.xsl and the stylesheet into
# DiagnosticProtocols. A flash runs through the programming executor, which records nothing there,
# so it is added as ODIS's flash screen adds it (FlashingResultModel.addFlashResultToProtocol): a new
# flash section, the unit's identification, the "006 Flashing" result, the identification after.
# The protocol never decides a job: every step of it is best effort.
PROTOCOL_DIR = r"C:\ProgramData\OE\DiagnosticProtocols"


def _protocol_start(diag):
    """Starts a new, empty automation protocol and returns its handler (None if ODIS refused)."""
    try:
        handler = diag.getDiagnosticsProtocolHandler()
        diag.discardProtocol()  # drops what an earlier job left; any state may be discarded
        diag.initProtocol()
        diag.startProtocol()
        return handler
    except (Exception, Throwable):
        traceback.print_exc()
        return None


def _protocol_store(diag, handler, kind, jid):
    """Stops the protocol and stores it as <kind>_<VIN>_<time>.xml (+ .html) in DiagnosticProtocols.
    The name of the .html goes into the job's data, where the page finds it however the job ended."""
    if handler is None:
        return None
    try:
        diag.stopProtocol()
        vin = handle_vin({}).get("vin")  # a valid VIN or None, so the name is always a file name
        if not os.path.isdir(PROTOCOL_DIR):
            os.makedirs(PROTOCOL_DIR)
        path = os.path.join(PROTOCOL_DIR, "%s_%s%s.xml" % (kind, vin + "_" if vin else "", time.strftime("%Y%m%dT%H%M%S")))
        handler.storeProtocol(path)
        html = path[:-4] + ".html"
        if not os.path.exists(html):
            return None
        _job_set(jid, data={"protocol": os.path.basename(html)})
        return os.path.basename(html)
    except (Exception, Throwable):
        traceback.print_exc()
        return None


def _protocol_model(handler):
    """The ODIS protocol model (ISelfDiagProtocolModel) behind the automation protocol handler."""
    f = handler.getClass().getDeclaredField("itsISelfDiagProtocolModel")
    f.setAccessible(True)
    return f.get(handler)


def _protocol_try(fn, *args):
    try:
        fn(*args)
    except (Exception, Throwable):
        traceback.print_exc()


def _protocol_unit(address):
    """ODIS's UI control unit (IControlUnit) of the unit at address, as its protocol entries need,
    or None when the vehicle project has no such unit."""
    from java.lang import Class
    unit_class = _bundle("de.volkswagen.odis.vaudas.business").loadClass(
        "de.volkswagen.odis.vaudas.business.zecret.UiControlUnitImpl")
    wrap = [c for c in Class.getConstructors(unit_class) if len(c.getParameterTypes()) == 1][0]
    for e in session_vehicle().getActualECUs() or []:
        u = wrap.newInstance([e])
        if u.getAddress() == address:
            return u
    return None


# Data transfer: ODIS-E's 010 Data transfer, "Write data block". The automation API's
# dataSetDownload writes a parameter dataset (an .xml with its PARAMETER_DATA, or a .zip with the
# data and its signature) to the unit. EXPERIMENTAL / UNTESTED, like flashing: a wrong dataset can
# disable functions of the unit until the right one is written again.

DATASET_EXTENSIONS = (".xml", ".zip")
DATASET_HEADER_MAX = 16 << 20
_PARAMETER_DATA = re.compile(r"<PARAMETER_DATA\b([^>]*)>([^<]*)<", re.I)
_XML_ATTR = re.compile(r"""([\w.-]+)\s*=\s*["']([^"']*)["']""")


def _dataset_file(cmd):
    path = cmd.get("file") or cmd.get("path")
    if not path:
        raise Exception("missing file (a parameter dataset .xml or .zip on the ODIS PC)")
    if not os.path.isfile(path):
        raise Exception("file not found on the ODIS PC: %s" % path)
    if not path.lower().endswith(DATASET_EXTENSIONS):
        raise Exception("a data transfer takes a parameter dataset .xml or .zip, not %s" % os.path.basename(path))
    return path


_ZDC_IDENT = ("DATEINAME", "DATEIID", "VERSION-INHALT", "DATUM", "BESCHREIBUNG")


def _zdc_ident(text):
    """The IDENT of a ZDC data container (part number DATEIID, content version, date, description)."""
    ident = {}
    for tag in _ZDC_IDENT:
        m = re.search(r"<IDENT>.*?<%s>(.*?)</%s>" % (tag, tag), text, re.S)
        if m:
            ident[tag] = m.group(1).strip()
    return ident


def _dataset_info(path):
    """What a dataset file holds, best effort: for an .xml its PARAMETER_DATA blocks (attributes
    such as DIAGNOSTIC_ADDRESS, START_ADDRESS, ZDC_NAME, ZDC_VERSION, LOGIN, and data size in bytes)
    and a ZDC container's IDENT; for a .zip its files and the IDENT of the ZDC container in it."""
    if path.lower().endswith(".zip"):
        import zipfile
        z = zipfile.ZipFile(path)
        try:
            names = z.namelist()
            info = {"entries": names, "blocks": []}
            for n in names:
                if n.lower().endswith(".xml") and z.getinfo(n).file_size <= DATASET_HEADER_MAX:
                    text = z.read(n).decode("latin-1")
                    if "<ZDC>" in text[:4096]:
                        info["zdc"] = _zdc_ident(text)
                        break
            return info
        finally:
            z.close()
    if os.path.getsize(path) > DATASET_HEADER_MAX:
        return {"blocks": []}
    f = open(path, "rb")
    try:
        text = f.read().decode("latin-1")
    finally:
        f.close()
    info = {"blocks": _dataset_blocks(text)}
    if "<ZDC>" in text[:4096]:
        info["zdc"] = _zdc_ident(text)
    return info


def _dataset_blocks(text):
    blocks = []
    for attrs, data in _PARAMETER_DATA.findall(text):
        block = dict((k.upper(), v) for k, v in _XML_ATTR.findall(attrs))
        block["bytes"] = len(re.findall(r"0x[0-9a-fA-F]{1,2}\b", data)) or len(re.sub(r"[^0-9a-fA-F]", "", data)) // 2
        blocks.append(block)
    return blocks


def handle_data_transfer_check(cmd):
    """Read-only check before a data transfer: the dataset file (size, its PARAMETER_DATA blocks
    and whether they are meant for the unit at cmd["address"]) and the datasets ODIS lists on the
    unit. Nothing is written."""
    try:
        path = _dataset_file(cmd)
        address = parse_address(cmd)
    except Exception as e:
        return {"ok": False, "error": unicode(e)}
    out = {"ok": True, "file": path, "size": os.path.getsize(path)}
    try:
        out.update(_dataset_info(path))
    except Exception as e:
        out["blocks"], out["info_error"] = [], unicode(e)
    targets = set()
    for b in out["blocks"]:
        try:
            targets.add(int(b.get("DIAGNOSTIC_ADDRESS", ""), 0))
        except ValueError:
            pass
    for n in out.get("entries", []):  # a ZDC zip's datasets are named DA_<unit address>_<...>
        m = re.match(r"DA_([0-9A-Fa-f]{3,4})_", os.path.basename(n))
        if m:
            targets.add(int(m.group(1), 16))
    if targets and address not in targets:
        out["warning"] = "the dataset is for unit address %s, not 0x%04X" % (
            ", ".join("0x%04X" % t for t in sorted(targets)), address)

    def read(diag, h):
        try:
            out["on_unit"] = [{"name": unicode(d.getDatasetName() or ""), "address": unicode(d.getDatasetAddress() or ""),
                               "version": unicode(d.getDatasetVersion() or "")}
                              for d in diag.getAvailableDatasetsOnEcu(h) or []]
        except (Exception, Throwable) as e:
            out["on_unit_error"] = error_text(e)
        return out
    return with_connection(cmd, read)


def handle_data_transfer_run(cmd):
    """EXPERIMENTAL / UNTESTED. Writes the parameter dataset "file" to the unit at "address"
    through IDiagnosticInterface.dataSetDownload, as a background job. ODIS reports no progress
    while it writes, only the result."""
    try:
        path = _dataset_file(cmd)
        address = parse_address(cmd)
    except Exception as e:
        return {"ok": False, "error": unicode(e)}

    def job(progress, jid):
        progress(3, "Selecting the unit...", "prepare")
        diag = Factory.getInstance()
        handle = diag.connectToEcu(address).getConnectionHandle()
        if _job_cancelled(jid):
            return {"cancelled": True, "stage": "before writing"}
        progress(10, "Writing the data block. Do not switch off or disconnect.", "writing")
        result = diag.dataSetDownload(handle, path)
        if result is not None and result.isErrorOccured():
            raise Exception("ODIS did not write the dataset: %s%s" % (
                result.getErrorMessage() or "unknown error",
                " (%s)" % result.getErrorID() if result.getErrorID() else ""))
        progress(100, "Data block written", "done")
        out = {"written": True, "file": path}
        if result is not None:
            out["link"] = unicode(result.getLogicalLinkname() or "")
        return out
    log_offset = _engine_log_size()
    return {"ok": True, "job_id": run_background(job, cmd), "log_offset": log_offset}


# ODIS logs every flash step (container loading, sessions, the programming sequence, errors) to
# its engine.log. flash_log returns the flash-related entries from a byte offset on (flash_run
# reports the offset at its start), so the page can show and keep them even if the flash fails.
ODIS_ENGINE_LOG = r"C:\ProgramData\OE\log\engine.log"
_LOG_ENTRY = re.compile(r"^(\d{8}) (\d\d:\d\d:\d\d):(\d{3}) \[(\w+)\s*\] \[[^\]]*\] ([\w$.]+)\([^)]*\) -> ?(.*)$")
_FLASH_LOG_KEEP = re.compile(r"flash|programm|sequenc|session|ecumem|download|transfer|dataset|erase|checksum|"
                             r"seed|security|fingerprint|routine|reset", re.I)
_FLASH_LOG_NOISE = re.compile(r"WLAN_STATUS|WlanConnection|MCD LogicalLink = null")
FLASH_LOG_READ_MAX = 8 << 20


def _engine_log_size():
    import os
    try:
        return os.path.getsize(ODIS_ENGINE_LOG)
    except OSError:
        return 0


def handle_flash_log(cmd):
    """Flash-related ODIS engine.log entries from byte "offset" on (no offset: the last 2 MB),
    and "next", the offset to continue from."""
    lines, next_offset = _flash_log_lines(int(cmd.get("offset", -1)))
    return {"ok": True, "lines": lines, "next": next_offset}


def _flash_log_lines(offset):
    """(lines, next offset): the flash-related engine.log entries from byte offset on. Errors and
    warnings are always kept; stack frames are dropped, "Caused by" lines kept."""
    size = _engine_log_size()
    partial = offset < 0 or offset > size  # unknown or log rolled over: start mid-file
    if partial:
        offset = max(0, size - (2 << 20))
    f = open(ODIS_ENGINE_LOG, "rb")
    try:
        f.seek(offset)
        data = f.read(min(size - offset, FLASH_LOG_READ_MAX))
    finally:
        f.close()
    end = data.rfind("\n") + 1  # only whole lines; the rest is read next time
    text = data[:end].decode("latin-1")
    lines, keep = [], False
    for i, line in enumerate(text.split("\n")):
        line = line.rstrip("\r")
        if i == 0 and partial and offset > 0:
            continue
        m = _LOG_ENTRY.match(line)
        if m:
            day, hms, ms, level, where, msg = m.groups()
            keep = not _FLASH_LOG_NOISE.search(line) and (
                level in ("ERROR", "WARN") or _FLASH_LOG_KEEP.search(where + " " + msg) is not None)
            if keep:
                where = ".".join(where.split(".")[-2:])
                lines.append(("%s.%s %-5s %s: %s" % (hms, ms, level, where, msg))[:600])
        elif keep and line.strip().startswith("Caused by"):
            lines.append("      " + line.strip()[:400])
    return lines[-3000:], offset + end



# --- Basic settings (Grundeinstellung) and output tests (Stellglieddiagnose) ----------------
# A basic setting runs a routine in the unit (throttle adaptation, sensor calibration...); an output
# test drives one of its actuators (a relay, a motor, a lamp). ODIS's automation interface treats both
# alike: the routine is initialized on one connection, started, checked repeatedly for its status
# and the chosen measurement values, and stopped. It runs as a background job so the page can show
# the status live and stop it; it also stops if the page stops polling (closed), so the unit is not
# left running it while holding the vehicle lock. A sequential output test (older units drive their
# actuators one after another) moves on with job_next.

# IDiagnosticInterface methods per kind: names, start parameters, measurements, create, init,
# start, check, stop, abort, next.
ROUTINES = {
    "basic": ("getShortNamesOfBasicSettings", "getShortNamesOfStartParametersForBasicSettings",
              "getShortNamesOfMeasurementsForBasicSettings", "createBasicSetting", "basicSettingInit",
              "basicSettingStart", "basicSettingCheck", "basicSettingStop", "basicSettingAbort", None),
    "output": ("getShortNamesOfActuators", "getShortNamesOfStartParametersForActuator",
               "getShortNamesOfMeasurementsForActuatorDiagnostics", "createDiagActuator",
               "actuatorDiagnosticsInit", "actuatorDiagnosticsStart", "actuatorDiagnosticsCheck",
               "actuatorDiagnosticsStop", "actuatorDiagnosticsAbort", "actuatorDiagnosticsNext"),
}


def _routine_list(kind):
    """The ODX short names of the unit's basic settings or output tests."""
    return lambda cmd: with_connection(cmd, lambda diag, h: names_result(getattr(diag, ROUTINES[kind][0])(h)))


def _routine_info(kind):
    """The start parameters of routine "name" and the measurements a check can read."""
    def handler(cmd):
        name = cmd.get("name")
        if not name:
            return {"ok": False, "error": "missing name"}
        m = ROUTINES[kind]
        return with_connection(cmd, lambda diag, h: {
            "ok": True, "name": name,
            "params": [unicode(n) for n in getattr(diag, m[1])(h, name) or []],
            "measurements": [unicode(n) for n in getattr(diag, m[2])(h) or []]})
    return handler


def _routine_run(kind):
    """Starts routine "name" with optional "params" (name -> value) and "measurements" (short names
    read with every check), then checks it every "interval" seconds until job_cancel, or until the
    page has not polled job_status for 20 s; job_next steps a sequential output test. The job's data
    is the latest status list; the result holds the start, last check and stop statuses. An optional
    login/session unlocks the unit first."""
    def handler(cmd):
        from java.util import ArrayList, HashMap
        name = cmd.get("name")
        if not name:
            return {"ok": False, "error": "missing name"}
        address = parse_address(cmd)
        params, measurements = cmd.get("params") or {}, cmd.get("measurements") or []
        interval = max(0.2, float(cmd.get("interval", 1)))
        create, init, start, check, stop, abort, step = ROUTINES[kind][3:]

        def job(progress, jid):
            diag = Factory.getInstance()
            handle = diag.connectToEcu(address).getConnectionHandle()
            opened = cmd.get("login") is not None or bool(cmd.get("session"))
            if opened:
                diag.openConnection(handle)
            names, out = ArrayList([name]), {}
            try:
                maybe_login(diag, handle, cmd)
                m = HashMap()
                for k, v in params.items():
                    m.put(k, unicode(v))
                routine = getattr(diag, create)(name, m, ArrayList(measurements))
                getattr(diag, init)(handle, ArrayList([routine]), ArrayList(measurements))
                progress(None, "running", "running")
                try:
                    status = out["start"] = to_data(getattr(diag, start)(handle, names))
                    _job_set(jid, data=status)
                    while not _job_cancelled(jid):
                        if _job_unpolled_for(jid) > 20:
                            out["stopped_because"] = "the page stopped polling"
                            break
                        time.sleep(interval)
                        if step and _job_take_next(jid):
                            status = out["last"] = to_data(getattr(diag, step)(handle))
                        else:
                            status = out["last"] = to_data(getattr(diag, check)(handle, names))
                        _job_set(jid, data=status)
                finally:
                    progress(None, "stopping", "stopping")
                    try:
                        out["stop"] = to_data(getattr(diag, stop)(handle, names))
                    finally:
                        getattr(diag, abort)(handle)
                return out
            finally:
                if opened:
                    try:
                        diag.closeConnection(handle)
                    except (Exception, Throwable):
                        pass
        return {"ok": True, "job_id": run_background(job, cmd)}
    return handler


def _job_take_next(jid):
    """Whether job_next was requested since the last call."""
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if not j or not j.get("next"):
            return False
        j["next"] = False
        return True


def handle_job_next(cmd):
    """Moves a running sequential output test on to its next actuator."""
    jid = cmd.get("job_id")
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j is None or j["status"] != "running":
            return {"ok": False, "error": "no such running job: %s" % jid}
        j["next"] = True
        return {"ok": True, "job_id": jid}


HANDLERS = {
    "_debug": handle__debug,
    "gfs_explore": handle_gfs_explore,
    "gf_identify": handle_gf_identify,
    "gf_functions": handle_gf_functions,
    "cp_probe": handle_cp_probe,
    "gf_run": handle_gf_run,
    "car_backup": handle_car_backup,
    "job_answer": handle_job_answer,
    "fault_guidance": handle_fault_guidance,
    "service_methods": handle_service_methods,
    "list_services": handle_list_services,
    "text_lookup": handle_text_lookup,
    "vehicle": handle_vehicle,
    "vin": handle_vin,
    "vci": handle_vci,
    "reset_vci": handle_reset_vci,
    "flash_config": handle_flash_config,
    "flash_run": handle_flash_run,
    "flash_log": handle_flash_log,
    "data_transfer_check": handle_data_transfer_check,
    "data_transfer_run": handle_data_transfer_run,
    "job_cancel": handle_job_cancel,
    "job_status": handle_job_status,
    "job_list": handle_job_list,
    "threads": handle_threads,
    "doip_activation": handle_doip_activation,
    "vehicle_projects": handle_vehicle_projects,
    "set_vehicle_project": handle_set_vehicle_project,
    "lookup_projects": handle_lookup_projects,
    "api_methods": handle_api_methods,
    "project_state": handle_project_state,
    "ping": handle_ping,
    "list_project_ecus": handle_list_project_ecus,
    "list_vehicle_ecus": handle_list_vehicle_ecus,
    "find_ecu": handle_find_ecu,
    "get_ecu_coding_info": handle_get_ecu_coding_info,
    "create_coding_job": handle_create_coding_job,
    "get_coding_items": handle_get_coding_items,
    "get_coding_structure": handle_get_coding_structure,
    "read_coding_values": handle_read_coding_values,
    "read_identification": handle_read_identification,
    "read_coding": handle_read_coding,
    "list_coding_values": handle_list_coding_values,
    "list_adaptation_channels": handle_list_adaptation_channels,
    "read_adaptation": handle_read_adaptation,
    "read_adaptation_values": handle_read_adaptation_values,
    "adaptation_options": handle_adaptation_options,
    "write_byte_coding": handle_write_byte_coding,
    "write_adaptation": handle_write_adaptation,
    "list_access_modes": handle_list_access_modes,
    "login": handle_login,
    "grp_login": handle_grp_login,
    "_grp_login_stub": handle__grp_login_stub,
    "list_measurement_groups": handle_list_measurement_groups,
    "list_measurements": handle_list_measurements,
    "read_measurement": handle_read_measurement,
    "read_measurement_group": handle_read_measurement_group,
    "read_gateway_component_list": handle_read_gateway_component_list,
    "read_event_memory": handle_read_event_memory,
    "list_basic_settings": _routine_list("basic"),
    "basic_setting_info": _routine_info("basic"),
    "basic_setting_run": _routine_run("basic"),
    "list_output_tests": _routine_list("output"),
    "output_test_info": _routine_info("output"),
    "output_test_run": _routine_run("output"),
    "job_next": handle_job_next,
    "clear_event_memory": handle_clear_event_memory,
    "odx_variants": handle_odx_variants,
    "odx_unit": handle_odx_unit,
    "odx_did_layouts": handle_odx_did_layouts,
    "odx_table_layout": handle_odx_table_layout,
    "gateway_nodes": handle_gateway_nodes,
    "odx_variant_patterns": handle_odx_variant_patterns,
    "odx_dtcs": handle_odx_dtcs,
    "odx_table": handle_odx_table,
    "odx_textual_variants": handle_odx_textual_variants,
    "coding_coverage": handle_coding_coverage,
}
