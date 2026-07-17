"""Build campus-map/map_data.json for the official BNBU/UIC hand-drawn map
(shouhuiditu2025.jpg, 5600x4000) + render a QA overlay for visual checking.

Building lat/lng come from OSM (ODbL); image xy read off grid-overlay tiles.
The hand-drawn map is NOT uniformly to scale: the IAS / sports-park zone is
drawn ~2.5x compressed vs the core campus, so:
  - calibration.gps uses core-campus pairs only (uniform ~0.15 m/px there)
  - long connector edges into the compressed zone carry explicit meters
"""
import json, math, os

SRC_IMG = '/tmp/srcdata/shouhuiditu2025.jpg'  # cp the official map here first (macOS TCC)
REPO = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- buildings
# (id, name, aliases, category, x, y, lat, lng, desc)
B = [
    # teaching / academic
    ('T1', 'T1 厚德楼', ['Business and Management Building', 'houde'], 'teaching', 4030, 2320, 22.351915, 113.515225, '工商管理学部，Business and Management Building。'),
    ('T2', 'T2 人文楼', ['Humanities Building', 'renwen'], 'teaching', 3660, 2360, 22.352354, 113.514868, '人文社科学部，Humanities Building。'),
    ('T3', 'T3 格物楼', ['Science Building', 'gewu'], 'teaching', 3330, 2310, 22.352716, 113.514551, '理工科技学部，Science Building。'),
    ('T4', 'T4 教学楼', ['Teaching Building'], 'teaching', 4150, 1520, 22.353008, 113.516789, '湖畔教学楼。'),
    ('T5', 'T5 教学楼', ['Teaching Building'], 'teaching', 3930, 1580, 22.353186, 113.516306, '教学楼。'),
    ('T6', 'T6 教学楼', ['Teaching Building'], 'teaching', 3680, 1690, 22.353349, 113.515783, '教学楼。'),
    ('T7', 'T7 教学楼', ['Teaching Building'], 'teaching', 3360, 1740, 22.353598, 113.515322, '教学楼。'),
    ('T8', 'T8 博简楼', ['CEFC Building', 'bojian'], 'teaching', 2980, 1800, 22.353813, 113.514806, 'CEFC Building，博简楼。'),
    ('T29', 'T29 教学楼', ['Teaching Building'], 'teaching', 2700, 1950, 22.353943, 113.513876, '教学楼。'),
    ('LRC', '学习资源中心', ['Learning Resource Centre', 'library', '图书馆', 'LRC'], 'library', 4180, 1910, 22.352488, 113.515779, '图书馆 / Learning Resource Centre，通宵自习区在此。'),
    # Da Tong Village 大同邨
    ('V15', 'V15 明心楼', ['Ming Xin House', '大同邨'], 'dorm', 3650, 1060, 22.354245, 113.516794, '大同邨宿舍。'),
    ('V16', 'V16 尚贤楼', ['Shang Xian House', '大同邨'], 'dorm', 3990, 1090, 22.353883, 113.516972, '大同邨宿舍。'),
    ('V17', 'V17 仁爱楼', ['Ren Ai House', '大同邨'], 'dorm', 3150, 1170, 22.354712, 113.515915, '大同邨宿舍。'),
    ('V18', 'V18 抱朴楼', ['Bao Pu House', '大同邨'], 'dorm', 3510, 1340, 22.354148, 113.516024, '大同邨宿舍。'),
    ('V19', 'V19 真知楼', ['Zhen Zhi House', '大同邨'], 'dorm', 2830, 1350, 22.354747, 113.515169, '大同邨宿舍。'),
    ('V20', 'V20 笃行楼', ['Du Xing House', '大同邨'], 'dorm', 3130, 1500, 22.354333, 113.515203, '大同邨宿舍。'),
    ('V21', 'V21 博文楼', ['Bo Wen House', '大同邨'], 'dorm', 2510, 1520, 22.354726, 113.514496, '大同邨宿舍。'),
    ('V22', 'V22 雅志楼', ['Ya Zhi House', '大同邨'], 'dorm', 2860, 1640, 22.354305, 113.514478, '大同邨宿舍。'),
    # Hui Xian Village 会贤邨
    ('V23', 'V23 雪芹楼', ['Xue Qin House', '会贤邨'], 'dorm', 2410, 1780, 22.354476, 113.513720, '会贤邨宿舍。'),
    ('V24', 'V24 阳明楼', ['Yang Ming House', '会贤邨'], 'dorm', 2180, 2040, 22.354363, 113.513352, '会贤邨宿舍。'),
    ('V25', 'V25 东坡楼', ['Dong Po House', '会贤邨'], 'dorm', 2810, 2260, 22.353160, 113.513903, '会贤邨宿舍。'),
    ('V26', 'V26 歌德楼', ['Goethe House', '会贤邨'], 'dorm', 2550, 2430, 22.353362, 113.513383, '会贤邨宿舍。'),
    ('V27', 'V27 雨果楼', ['Hugo House', '会贤邨'], 'dorm', 2080, 2630, 22.353508, 113.512679, '会贤邨宿舍。'),
    ('V28', 'V28 莎翁楼', ['Shakespeare House', '会贤邨'], 'dorm', 3090, 2750, 22.352459, 113.513648, '会贤邨宿舍。'),
    ('V29', 'V29 容闳楼', ['Yung Wing House', '会贤邨'], 'dorm', 2550, 2630, 22.352977, 113.513183, '会贤邨宿舍。'),
    # admin / landmark
    ('HALL', '大学会堂', ['University Hall'], 'admin', 4720, 2690, 22.351059, 113.515775, '大型典礼与演出场地。'),
    ('ADMIN', '行政楼', ['Administration Building'], 'admin', 4970, 2630, 22.350959, 113.516116, '行政办公楼。'),
    ('THEA', '演艺厅', ['Performance Theatre'], 'landmark', 4640, 2140, 22.351373, 113.516394, 'Performance Theatre。'),
    ('GYM', '体育馆', ['Sports Complex', 'gym'], 'sports', 4010, 2770, 22.351549, 113.514710, '室内体育馆 / Sports Complex。'),
    ('CCC', '文化创意群落', ['Cultural Creativity Clusters', '文创'], 'landmark', 4800, 1700, 22.351782, 113.517439, 'Cultural Creativity Clusters。'),
    ('ARTS', '艺峰', ['Arts Hill', '艺峯'], 'landmark', 4310, 910, 22.353707, 113.517930, 'Arts Hill 艺术空间。'),
    ('PARK', '体育公园', ['Sports Park', '田径场', '操场'], 'sports', 2000, 860, 22.359850, 113.513700, '田径场与球场群。'),
    ('IAS', '高等研究院', ['Institute for Advanced Study', 'IAS'], 'admin', 840, 950, 22.365550, 113.511050, '高等研究院园区（D1-D6）。'),
    ('D1', 'D1 (高研院)', ['IAS D1'], 'dorm', 985, 545, None, None, '高等研究院园区建筑。'),
    ('D2', 'D2 (高研院)', ['IAS D2'], 'dorm', 1245, 745, None, None, '高等研究院园区建筑。'),
    ('D3', 'D3 (高研院)', ['IAS D3'], 'dorm', 1215, 1090, None, None, '高等研究院园区建筑。'),
    ('D4', 'D4 (高研院)', ['IAS D4'], 'dorm', 710, 1275, None, None, '高等研究院园区建筑。'),
    ('D5', 'D5 (高研院)', ['IAS D5'], 'dorm', 455, 1010, None, None, '高等研究院园区建筑。'),
    ('D6', 'D6 (高研院)', ['IAS D6'], 'dorm', 600, 700, None, None, '高等研究院园区建筑。'),
    ('HTV', '会同村', ['Huitong Village', '会同古村'], 'landmark', 1430, 2150, 22.356408, 113.511471, '毗邻校园的百年古村，咖啡馆与展馆聚集。'),
    ('YZ', '榆栈食堂', ['YuZhan', '会贤食堂', 'canteen'], 'dining', 2380, 2220, 22.353115, 113.512516, '会贤邨侧餐饮楼。'),
    ('EAT1', '餐饮点·湖畔', ['canteen', '食堂'], 'dining', 4250, 1320, None, None, '官方手绘图标注的餐饮位置。'),
    ('EAT2', '餐饮点·演艺厅旁', ['canteen', '食堂'], 'dining', 4530, 1970, None, None, '官方手绘图标注的餐饮位置。'),
    ('EAT3', '餐饮点·高研院', ['canteen', '食堂'], 'dining', 750, 440, None, None, '官方手绘图标注的餐饮位置。'),
    ('GATE', '正门', ['Main Gate', '南门', '校门'], 'gate', 4390, 3060, 22.350300, 113.515600, '金同路主入口，邻近“浸会大学东 / 北师港浸大”公交站。'),
    ('GATE2', '西门 (明德路)', ['West Gate', '明德路', '校门'], 'gate', 1680, 2985, 22.351800, 113.513600, '明德路与金同路交汇处的西侧入口，去会同村和二期（高研院）从这里走大路。'),
]

# ------------------------------------------------------- calibration (core!)
CAL_IDS = ['T1', 'T8', 'HALL', 'ADMIN', 'LRC', 'V15', 'V23', 'V27', 'ARTS', 'GYM']

# ---------------------------------------------------------------- path graph
# node id -> [x, y]  (image px)
N = {
    # south spine (campus south road, parallel to Jintong Rd)
    's1': [1990, 2950], 's2': [2550, 2980], 's3': [3090, 2990], 's4': [3600, 2990],
    's5': [4010, 2990], 's6': [4390, 2960], 's7': [4970, 2950],
    'gate': [4390, 3060],
    # central axis (gate -> plaza -> theatre forecourt)
    'x1': [4390, 2700], 'x2': [4390, 2420], 'x3': [4390, 2150],
    # mid E-W road (north of T1/T2/T3)
    'm1': [2760, 2150], 'm2': [3100, 2200], 'm3': [3340, 2190], 'm4': [3660, 2180],
    'm5': [4040, 2160],
    # north lane (through Da Tong dorm rows)
    'nl1': [4050, 790], 'nl2': [3450, 870], 'nl3': [3150, 990], 'nl4': [2880, 1100],
    'vc1': [3460, 1150], 'ec1': [4080, 1000],
    # boulevard (T4 fork -> T29, between dorms and teaching row)
    'bd1': [4160, 1270], 'bd2': [3860, 1370], 'bd3': [3560, 1440], 'bd4': [3260, 1510],
    'bd5': [3000, 1600], 'bd6': [2800, 1720],
    'd1': [4180, 980],
    'd7': [2540, 2060],
    # Hui Xian roundabout + spokes
    'r1': [2280, 2380], 'hx1': [2280, 2700], 'hx2': [2060, 2500], 'hx3': [2600, 2500],
    # lake loop + bridge + theatre forecourt
    'l1': [4120, 1700], 'l2': [4170, 1440], 'l3': [4400, 1070], 'l4': [4830, 1130],
    'l5': [5060, 1520], 'l6': [4900, 1950], 'br1': [4600, 1670],
    'tn': [4640, 2230], 'ls': [4450, 2060],
    'e2': [5010, 2400],
    # main road 一期<->二期 (浸会大学路 -> 金同路, drawn WEST of Huitong Village)
    # and the 明德路 west entrance; these are the only cross-phase connectors.
    'r2': [980, 1400],
    'mr1': [1030, 1520], 'mr2': [990, 1700], 'mr3': [900, 1870], 'mr4': [810, 2030],
    'mr5': [750, 2200], 'mr6': [720, 2420], 'mr7': [760, 2680],
    'wj': [920, 2950], 'wj2': [1650, 2960],
    'w1': [1650, 2450],
    # small paths confirmed by locals: east of Huitong Village + sports park
    'w2': [1250, 1800], 'sp2': [2260, 1420], 'sp3': [2380, 1650],
    # sports park spur off 浸会大学路 + IAS ring
    'pk': [1250, 880], 'sp': [2080, 1010],
    'i1': [845, 600], 'i2': [1130, 770], 'i3': [1170, 1120], 'i4': [840, 1280],
    'i5': [505, 1110], 'i6': [520, 760],
}

# edges: [a, b] or [a, b, meters] (real-world override for compressed zones)
E = [
    # south spine
    ['s1', 's2'], ['s2', 's3'], ['s3', 's4'], ['s4', 's5'], ['s5', 's6'], ['s6', 's7'],
    ['s6', 'gate'],
    # central axis
    ['s6', 'x1'], ['x1', 'x2'], ['x2', 'x3'], ['x3', 'm5'], ['x3', 'tn'],
    # mid E-W road
    ['m1', 'm2'], ['m2', 'm3'], ['m3', 'm4'], ['m4', 'm5'], ['m5', 'x2'],
    ['m5', 'ls'], ['ls', 'tn'],
    # boulevard + north lane + verticals
    ['bd1', 'bd2'], ['bd2', 'bd3'], ['bd3', 'bd4'], ['bd4', 'bd5'], ['bd5', 'bd6'],
    ['bd6', 'd7'], ['d7', 'r1'], ['bd6', 'm1'],
    ['nl1', 'nl2'], ['nl2', 'nl3'], ['nl3', 'nl4'],
    ['nl2', 'vc1'], ['vc1', 'bd3'], ['nl1', 'ec1'], ['ec1', 'bd1'],
    ['d1', 'nl1'], ['d1', 'ec1'],
    # Hui Xian roundabout spokes
    ['r1', 'hx1'], ['r1', 'hx2'], ['r1', 'hx3'], ['hx1', 's2'], ['hx2', 's1'],
    ['hx3', 's3'],
    # lake loop / bridge / east side
    ['bd1', 'l2'], ['l2', 'l1'], ['l1', 'm5'], ['bd1', 'l3'], ['d1', 'l3'],
    ['l3', 'l4'], ['l4', 'l5'], ['l5', 'l6'], ['l6', 'tn'], ['l6', 'e2'],
    ['e2', 's7'], ['l2', 'br1'], ['br1', 'l5'], ['br1', 'l6'],
    # 明德路 west entrance (inside campus)
    ['w1', 'r1'], ['w1', 'wj2', 120], ['wj2', 's1', 120],
    # small paths (confirmed to exist): east of the village + park -> dorms.
    # 4th element tags them as 'path' so the route card can flag 小路段.
    ['r2', 'w2', 820, 'path'], ['w2', 'w1', 340, 'path'],
    ['sp', 'sp2', 600, 'path'], ['sp2', 'sp3'], ['nl4', 'sp2'],
    ['sp3', 'r1', 210], ['sp3', 'd7', 200],
    # sports park spur + IAS ring (real meters; drawing is compressed here)
    ['sp', 'pk', 330], ['pk', 'r2', 370],
    ['i1', 'i2', 95], ['i2', 'i3', 95], ['i3', 'i4', 95], ['i4', 'i5', 95],
    ['i5', 'i6', 95], ['i6', 'i1', 95], ['i3', 'r2', 180],
]

# The 一期<->二期 main-road chain: 浸会大学路 from the IAS junction (r2) south,
# joining 金同路 SW of Huitong Village (wj), east to the 明德路 entrance (wj2).
# Real walking length ~1700 m (OSM: 浸会大学路 22.3638..22.3563 join 金同路
# .. 明德路口 22.3518,113.5136); distribute over segments by image length.
MAIN_ROAD_CHAIN = ['r2', 'mr1', 'mr2', 'mr3', 'mr4', 'mr5', 'mr6', 'mr7', 'wj', 'wj2']
MAIN_ROAD_METERS = 1700.0
_px = [math.hypot(N[a][0]-N[b][0], N[a][1]-N[b][1])
       for a, b in zip(MAIN_ROAD_CHAIN, MAIN_ROAD_CHAIN[1:])]
_total = sum(_px)
for (a, b), seg in zip(zip(MAIN_ROAD_CHAIN, MAIN_ROAD_CHAIN[1:]), _px):
    E.append([a, b, round(MAIN_ROAD_METERS * seg / _total)])

# building -> entrance nodes
ENTR = {
    'T1': ['m5'], 'T2': ['m4'], 'T3': ['m3'], 'T4': ['bd1'], 'T5': ['bd2'],
    'T6': ['bd3'], 'T7': ['bd4'], 'T8': ['bd5'], 'T29': ['bd6', 'm1'],
    'LRC': ['l1', 'm5', 'ls'],
    'V15': ['nl2', 'vc1'], 'V16': ['ec1'], 'V17': ['nl3'], 'V18': ['vc1', 'bd3'],
    'V19': ['nl4'], 'V20': ['bd4'], 'V21': ['sp3', 'nl4'], 'V22': ['bd5'],
    'V23': ['sp3', 'd7'], 'V24': ['r1'], 'V25': ['hx3', 'd7'], 'V26': ['hx3'],
    'V27': ['hx2'], 'V28': ['s3'], 'V29': ['hx1'],
    'HALL': ['x1'], 'ADMIN': ['e2', 's7'], 'THEA': ['tn', 'l6'], 'GYM': ['s5', 'x1'],
    'CCC': ['br1', 'l5', 'l6'], 'ARTS': ['d1'], 'PARK': ['sp'], 'IAS': ['i3'],
    'D1': ['i1', 'i2'], 'D2': ['i2'], 'D3': ['i3'], 'D4': ['i4'], 'D5': ['i5'],
    'D6': ['i6'], 'HTV': ['mr4'], 'YZ': ['r1', 'hx2'],
    'EAT1': ['bd1'], 'EAT2': ['ls'], 'EAT3': ['i1'], 'GATE': ['gate'],
    'GATE2': ['wj2'],
}

# D1-D6 / EAT3 have no OSM names, and the core-campus affine cannot be
# extrapolated into the compressed IAS zone (it would land ~1 km off). Derive
# their real coords from the IAS cluster center + the building's compass angle
# on the image ring (ring radius ~100 m on the ground).
IAS_IMG = (840, 950)
IAS_REAL = (22.365550, 113.511050)
RING_M = 100.0
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LNG = 111320.0 * math.cos(math.radians(IAS_REAL[0]))

def ias_ring_latlng(x, y, radius_m=RING_M):
    ang = math.atan2(-(y - IAS_IMG[1]), x - IAS_IMG[0])  # image y-down -> north-up
    return (round(IAS_REAL[0] + radius_m * math.sin(ang) / M_PER_DEG_LAT, 6),
            round(IAS_REAL[1] + radius_m * math.cos(ang) / M_PER_DEG_LNG, 6))

B = [(bid, name, al, cat, x, y, *((lat, lng) if lat is not None else
      ias_ring_latlng(x, y, 130 if bid == 'EAT3' else RING_M)
      if bid in ('D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'EAT3') else (lat, lng)), desc)
     for bid, name, al, cat, x, y, lat, lng, desc in B]

buildings = []
by_id = {}
for bid, name, aliases, cat, x, y, lat, lng, desc in B:
    o = {'id': bid, 'name': name, 'aliases': aliases, 'category': cat,
         'x': x, 'y': y, 'nodes': ENTR.get(bid, []), 'desc': desc}
    if lat is not None:
        o['lat'], o['lng'] = lat, lng
    buildings.append(o)
    by_id[bid] = o

gps = [{'x': by_id[i]['x'], 'y': by_id[i]['y'],
        'lat': by_id[i]['lat'], 'lng': by_id[i]['lng']} for i in CAL_IDS]

data = {
    'image': {'src': 'map.webp?v=2025', 'width': 5600, 'height': 4000},
    'credit': '手绘地图 © 北师香港浸会大学 · 坐标数据 © OpenStreetMap 贡献者 · 路径人工标注，仅供参考',
    'calibration': {'metersPerPixel': None, 'gps': gps},
    'buildings': buildings,
    'nodes': N,
    'edges': E,
}

# sanity checks
node_ids = set(N)
for a, *rest in E:
    b = rest[0]
    assert a in node_ids and b in node_ids, (a, b)
for bid, nodes in ENTR.items():
    assert bid in by_id, bid
    for n in nodes:
        assert n in node_ids, (bid, n)

with open(os.path.join(REPO, 'campus-map/map_data.json'), 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

# report derived meters-per-pixel from calibration pairs (core zone)
def hav(a, b, c, d):
    R, r = 6371000, math.pi / 180
    return 2 * R * math.asin(math.sqrt(math.sin((c - a) * r / 2) ** 2 +
        math.cos(a * r) * math.cos(c * r) * math.sin((d - b) * r / 2) ** 2))
tot, cnt = 0, 0
for i in range(len(gps)):
    for j in range(i + 1, len(gps)):
        px = math.hypot(gps[i]['x'] - gps[j]['x'], gps[i]['y'] - gps[j]['y'])
        if px < 200: continue
        tot += hav(gps[i]['lat'], gps[i]['lng'], gps[j]['lat'], gps[j]['lng']) / px
        cnt += 1
print(f'metersPerPixel (derived, core): {tot/cnt:.4f} over {cnt} pairs')

# ------------------------------------------------------------- QA overlay
if not os.path.exists(SRC_IMG):
    print('source image absent, skipping QA overlay')
    raise SystemExit(0)
from PIL import Image, ImageDraw
im = Image.open(SRC_IMG).convert('RGB')
d = ImageDraw.Draw(im)
for a, *rest in E:
    b = rest[0]
    d.line([tuple(N[a]), tuple(N[b])], fill=(255, 40, 40), width=14)
for nid, (x, y) in N.items():
    d.ellipse([x-22, y-22, x+22, y+22], fill=(255, 255, 0), outline=(0, 0, 0), width=4)
    d.text((x + 26, y - 30), nid, fill=(120, 0, 160))
for b in buildings:
    x, y = b['x'], b['y']
    d.ellipse([x-26, y-26, x+26, y+26], fill=(0, 90, 255), outline=(255, 255, 255), width=5)
    d.text((x + 30, y + 8), b['id'], fill=(0, 0, 190))
im.resize((2100, 1500), Image.LANCZOS).save('/tmp/campus_map_qa.png')
print('QA overlay written to /tmp/campus_map_qa.png')
