# POI 类别规则 v2 全表审查

日期：2026-10-12。范围：逐条重审 v1 的全部 602 行；601 个官方字典类别及 1 条既有充电设施例外。判定以本轮用户研究目的和标准为准，旧决定和旧理由（含“用户已确认”）只作为被审查材料。

研究描述充电站周边的活动场所、生产及服务设施；不以独立经营、有人值守、公众开放、直接服务充电者或证明充电需求因果作为统一门槛。

## 交付及原始内容保留

- [v2 完整规则](poi_research_rules_v2.csv)：与 v1 相同的 12 个字段及原始 602 行顺序；仅 decision、decision_reason、必要的 poi_function_group 和 rule_version 调整。
- [完整变更清单](poi_research_rules_v2_changes.csv)：602 行均列原/新决定、原/新功能组、原理由、中文新依据及变更字段；review_scope 区分实质决定/组别变化和仅理由/版本变化。
- [功能组问题清单](poi_research_rules_v2_group_issues.csv)：纳入明确但原组冲突或归组需核对的项目，全部保持 include；建议列不是已实施改组。
- [结构及一致性核验](poi_research_rules_v2_validation.json)：源摘要、行列保留、明确决定、分类数量、同类检查和产物摘要。

v1 SHA-256：`2b097171084c895204e49d3185b7c064c25987e6e76b3a07cd6fde45aa21d465`。全部代码按文本写出并保留前导零；使用 UTF-8、标准 CSV 引号和 LF。表格软件导入时应将代码列指定为文本。

原始八个身份/来源字段逐值不变，包括 rule_origin；该历史字段不证明本轮决策由旧用户指令授权。本轮审查依据另在变更清单 review_authority 记录。rule_version 为 poi-research-v2.0.0，旧版本和所有旧理由完整保存在变更清单及未覆盖的 v1 中。

10540832 的原始 class_name 为空，v2 与变更清单均保留空值。其充电设施身份线索仅来自原表理由；本次 exclude 的授权来自本轮 prompt 的明确决定，不伪造官方名称。

## 数量

|范围|include|exclude|review|合计|
|---|---|---|---|---|
|v1 全部行|549|34|19|602|
|v2 全部行|578|17|7|602|
|v2 官方字典行|578|16|7|601|
|既有充电设施例外|0|1|0|1|

每行均重新审查并写入中文具体依据。决定发生变化 34 行，功能组发生变化 31 行；二者并集 37 行。其余行保持原决定/功能组但更新理由和版本，不把全部理由改写说成全部类别改变纳入。

## 决定或功能组变化

|代码|原始类别名称|原决定 → 新决定|原功能组 → 新功能组|本次依据|
|---|---|---|---|---|
|01010002|CAMPING, CARAVANNING, MOBILE HOMES, HOLIDAY PARKS AND CENTRES|review → include|（空） → accommodation|住宿类中的露营地、房车营地、移动住宅及度假园区提供住宿或度假活动空间；按本次明确决定纳入住宿组，不以是否独立经营或已核验开放状态为门槛。|
|02030047|CONSTRUCTION PLANT|review → exclude|（空） → （空）|本次 prompt 明确要求排除 CONSTRUCTION PLANT，按该项指定决定执行；不把这项排除扩展到另列的施工服务或工程设备租赁类别。|
|02090141|CASH MACHINES|exclude → include|（空） → office_services|ATM 直接提供取现等金融便民服务；按本次明确决定纳入办公与服务组，不以无人值守或附属于银行、商店为由排除。|
|02090811|PAYPOINT LOCATIONS|exclude → include|（空） → office_services|PayPoint 提供缴费及支付便民服务；按本次明确决定纳入办公与服务组，不因位于商店内或与宿主同址而排除整个类别。|
|02100156|HAIR AND BEAUTY SERVICES|include → include|retail → office_services|理发美容主要提供生活服务；按本次明确决定归办公与服务，不因设有门店或接待消费者而归零售。|
|02100170|LOCK, KEY AND SECURITY SERVICES|include → include|retail → office_services|锁具、钥匙和安防服务以开锁、配钥匙及安全技术服务为主；按本次明确决定归办公与服务。|
|02100175|PERSONALISATION|include → include|retail → office_services|个性化定制主要提供加工和定制服务；按本次明确决定归办公与服务，不因交付商品而一律归零售。|
|03160232|BIRD RESERVES, COLLECTIONS AND SANCTUARIES|review → include|（空） → leisure_tourism|景点大类中的鸟类保护区、鸟类展示与庇护场所具有保育及观赏功能；不因缺少收费、管理或开放证明而排除。|
|03170240|ARCHAEOLOGICAL SITES|review → include|（空） → leisure_tourism|考古遗址属于明确文化遗址；无需证明配有游客中心、收费或有人管理即可纳入。|
|03170241|BATTLEFIELDS|review → include|（空） → leisure_tourism|历史文化子类中的战场遗址承载历史纪念与文化场所功能，不按普通地理定位点排除。|
|03170245|HISTORIC AND CEREMONIAL STRUCTURES|review → include|（空） → leisure_tourism|历史及礼仪构筑物具有文化纪念或仪式功能，纳入文化场所；不因体量小或无人值守当作普通测量标记。|
|03180252|COMMONS|review → include|（空） → leisure_tourism|原生游憩子类中的公地作为开放休闲活动空间纳入；不因没有经营者或设施开放证明而排除。|
|03200269|UNSPECIFIED AND OTHER ATTRACTIONS|review → include|（空） → leisure_tourism|虽未细分类型，但原生旅游子类明确标为其他景点；不因缺少更细分类或开放证明而排除其旅游活动功能。|
|06340433|ELECTRICAL FEATURES|exclude → review|（空） → （空）|公共基础设施子类仅给出 Electrical features 泛称，未区分实际电力运营设施与纯网络构件代表点；不能沿用无人值守即排除的理由，也无法从现有表确定纳入侧边界，需核对类别定义或记录子型。|
|06340437|GAS FEATURES|exclude → review|（空） → （空）|Gas features 的现有层级未区分具有供气运营功能的设施与单纯燃气网络构件；两者纳入标准不同，现有表缺少可核验的对象细分，集中待确认。|
|06340438|METEOROLOGICAL FEATURES|exclude → review|（空） → （空）|Meteorological features 未区分承担观测服务的设施与仅作技术或定位标记的对象；需核对该类对象范围，不能凭是否有人值守决定，也不编造具体子类。|
|06340443|TELECOMMUNICATIONS FEATURES|exclude → review|（空） → （空）|Telecommunications features 未区分直接提供通信服务的设施与纯网络构件代表点；WiFi 服务点和电信公司另列不能证明本类全部应排除，需确认实际类别定义。|
|06340455|DRINKING FOUNTAINS AND WATER POINTS|exclude → include|（空） → education_health_public|饮水喷泉及供水点直接提供饮水便民服务；无人值守、体量小或附属于公园不构成类别排除。|
|06340457|LETTER BOXES|exclude → include|（空） → education_health_public|邮筒直接提供投递邮件服务，按便民设施纳入；不因与邮局或商店同址而整类排除。|
|06340461|PUBLIC TOILETS|exclude → include|（空） → education_health_public|公厕直接提供卫生便民服务，按本次标准纳入；是否收费、有人值守或独立经营均不是门槛。|
|06340802|WIFI HOTSPOTS|exclude → include|（空） → （空）|WiFi 热点直接提供上网接入服务，不能以设备或附属服务点为由排除；纳入已确定，办公通信服务与公共便民服务的功能组边界另列核对，暂保留原空值。|
|07380507|UNSPECIFIED QUARRIES OR MINES|review → include|（空） → industry_logistics|未细分的采石场或矿山仍在采掘工业子类中明确指向生产场所；不凭可能废弃的猜测整类排除，过时记录留待点位证据处理。|
|07390521|SHEEP DIPS AND WASHES|exclude → include|（空） → industry_logistics|羊只药浴及清洗设施直接承担养殖卫生和生产支持功能；不以是否独立经营或有人值守作为排除理由。|
|07410534|ENERGY PRODUCTION|review → include|（空） → industry_logistics|能源生产直接具有生产功能，无人值守的发电设施也不因人员条件被排除；与纯输送管线等工程网络结构区分。|
|07410536|LIME KILNS|review → include|（空） → industry_logistics|石灰窑在工业特征子类中指明石灰生产功能；不以未经核实的闲置或历史状态猜测整类排除。|
|07410537|OAST HOUSES|review → include|（空） → industry_logistics|啤酒花烘干房在制造业工业特征子类中具有干燥加工用途；不凭可能改用或废弃的推测排除，具体记录状态另行核验。|
|07410542|UNSPECIFIED WORKS OR FACTORIES|review → include|（空） → industry_logistics|未细分的工厂或生产场所仍明确具有生产属性；缺少产品细分或当前营业证明不构成类别排除。|
|07410543|WATER PUMPING STATIONS|exclude → include|（空） → industry_logistics|水泵站是执行抽送供水等运行功能的明确厂站，按生产服务设施保留，不因无人值守或不对外开放排除。|
|10530728|AIRPORTS AND LANDING STRIPS|review → include|（空） → transport_parking|机场和起降跑道均具有航空运输起降功能；不要求公共客运、独立经营或公众自由进入，不能据私人起降场的可能性整类排除。|
|10530729|HELIPADS|exclude → include|（空） → transport_parking|直升机停机坪直接提供航空起降服务，属于明确交通设施；无人值守或附属于医院等机构不是排除理由。|
|10540744|WEIGHBRIDGES|exclude → include|（空） → transport_parking|地磅直接提供车辆或货物称重服务，属于明确运输生产支持设施；不以设备、附属或无人值守为由排除。|
|10560752|LOCKS|exclude → include|（空） → transport_parking|船闸执行船舶过闸和水位转换的航运运行服务，作为明确水运运行设施纳入；不因工程形态或无人值守排除，与仅表示桥槽构件的记录区分。|
|10560753|MOORINGS AND UNLOADING FACILITIES|review → include|（空） → transport_parking|系泊和装卸设施直接提供停靠及货物转运服务，两种功能均在本次范围内；客运与货运差别不构成 review 或排除理由。|
|10570731|BUS AND COACH STATIONS, DEPOTS AND COMPANIES|review → include|（空） → transport_parking|公交及长途客车站、车库和公司分别提供客运、运营维护或运输组织功能，均在本次范围内；不以公众能否进入运营场站为门槛。|
|10570794|LONDON UNDERGROUND ENTRANCES|exclude → include|（空） → transport_parking|伦敦地铁入口直接提供客运进出服务，应纳入类别范围；同站多个入口是否合并计数留待实际记录阶段统一处理，不能整类排除。|
|10590732|BUS STOPS|exclude → include|（空） → transport_parking|公交站提供明确候车及上下客服务，按本次标准纳入；站牌形态、无人值守或规模小不构成排除理由。|
|10590759|HAIL AND RIDE ZONES|exclude → include|（空） → transport_parking|招手即停区域提供明确的公交上下客服务位置，应纳入服务范围；区域或重复点的计数单位留待点位处理，不因不是独立门店而排除。|

## 仍需确认的纳入边界

以下 review 表示活动/服务对象与排除对象无法由现有类别信息分开，不表示要求营业或开放证明。四个泛称 features 类的具体子型尚无定义证据，不能断言实际交付一定包含某一种设施；保留 review 是避免把整个泛称任意等同于纯构件或服务厂站。这里只提出类别定义/记录子型问题，不逐条要求实地核验。

|代码|原始类别名称|待确认原因|
|---|---|---|
|03190257|DESIGNATED SCENIC FEATURES|景点—地景子类的指定风景要素未区分游憩观景目的地与仅表示自然地貌的标记；两者按本次标准处理不同，现有类别层级和理由不足以拆分，需核对对象定义或记录子型。|
|06340433|ELECTRICAL FEATURES|公共基础设施子类仅给出 Electrical features 泛称，未区分实际电力运营设施与纯网络构件代表点；不能沿用无人值守即排除的理由，也无法从现有表确定纳入侧边界，需核对类别定义或记录子型。|
|06340437|GAS FEATURES|Gas features 的现有层级未区分具有供气运营功能的设施与单纯燃气网络构件；两者纳入标准不同，现有表缺少可核验的对象细分，集中待确认。|
|06340438|METEOROLOGICAL FEATURES|Meteorological features 未区分承担观测服务的设施与仅作技术或定位标记的对象；需核对该类对象范围，不能凭是否有人值守决定，也不编造具体子类。|
|06340443|TELECOMMUNICATIONS FEATURES|Telecommunications features 未区分直接提供通信服务的设施与纯网络构件代表点；WiFi 服务点和电信公司另列不能证明本类全部应排除，需确认实际类别定义。|
|10560754|RIVERS AND CANAL ORGANISATIONS AND INFRASTRUCTURE|本类别明确同时包含河流运河组织及基础设施：运营服务机构应纳入，纯工程网络构件应排除；现有表没有子型可分离这两类，需集中核对。|
|10570738|RAILWAY STATIONS, JUNCTIONS AND HALTS|该类明确混合客运铁路车站、小站与铁路交汇点；前者有活动服务功能，后者可仅为网络连接代表点，现有类别无法区分，需核对记录子型而非要求公众开放证明。|

## 排除类别

|代码|原始类别名称|排除依据|
|---|---|---|
|02030047|CONSTRUCTION PLANT|本次 prompt 明确要求排除 CONSTRUCTION PLANT，按该项指定决定执行；不把这项排除扩展到另列的施工服务或工程设备租赁类别。|
|03160237|SALMON LADDERS|虽位于景点的动植物子类，SALMON LADDERS 指鱼类洄游通道工程构件；类别本身未指向水族馆、保护区或游览服务场所，按纯工程构件排除。|
|03190259|TRIGONOMETRIC POINTS|三角测量点用于测绘定位，属于本次明确排除的定位标记，不是文化遗址或游憩场所。|
|03580804|LAKES AND WATERS|水体子类的湖泊及水域记录表示水体对象本身，未表示公园、景区或水上活动服务场所。|
|03580805|LOCHS AND LOCHANS|水体子类中的大小湖泊表示自然水体对象，按纯地理对象排除；另列的游憩场所不受影响。|
|03580806|TARNS, POOLS AND MERES|山地小湖、水池和浅湖在水体子类中表示水体对象，不等同于游泳池等明确活动设施。|
|03580807|RESERVOIRS|本类位于水体子类，记录水库水体本身；不将其替代为供水厂站或游憩场所，后两者按各自功能类别保留。|
|07410538|PIPELINES|管线表示物质输送网络的工程结构，属于本次明确排除的纯工程构件；相关生产厂站及运营机构按各自类别保留。|
|10540730|BRIDGES|桥梁属于本次明确排除的交通网络工程构件；类别未表示客运场站或直接服务点。|
|10540733|CATTLE GRIDS|拦畜格栅是道路工程构件，本次明确要求不计入普通活动和服务设施构成。|
|10540734|FORDS AND LEVEL CROSSINGS|涉水通道和铁路平交道口表示道路通行结构或交叉点，按纯工程及网络结构代表点排除。|
|10540740|SIGNALLING FACILITIES|信号设施表示交通控制设备或网络结构点，属于本次明确排除的信号工程设施；另列的实际运营场站保留。|
|10540743|VIADUCTS|高架桥属于交通网络工程构件，与桥梁采用相同排除标准。|
|10540832|（空）|本次 prompt 明确排除电动汽车充电设施的普通 POI 构成；该身份线索来自原表理由中的 Public Electric Vehicle Charging Locations，原始 class_name 为空且原样保留，不伪造官方名称。邻近充电设施可另作集聚指标。|
|10550747|FOOTBRIDGES|步行桥仍是通行网络工程构件，与其他桥梁采用一致排除标准，不能仅因可供行人使用就当作活动服务场所。|
|10560751|AQUEDUCTS|渡槽表示输水或运河通行的工程构件，类别本身不表示供水厂站、码头或服务设施。|
|10560755|WEIRS, SLUICES AND DAMS|堰、泄水闸和坝表示水工调控构件，未表示发电厂、泵站、客货运场站或直接便民服务点，按纯工程结构排除。|

## 功能组冲突及未分组纳入项

功能组体系沿用原表。落实用户明确的六项 include 分组，其中三项原有组从 retail 改为 office_services，其余三项原为空；新增 include 的明确功能按现有组填写。其他原有非空组不擅自改写，明显冲突和边界单独列出。

|代码|原始类别名称|v2 组|问题类型|建议或候选（未执行）|原因|
|---|---|---|---|---|---|
|02030779|BUILDING AND COMPONENT SUPPLIERS|office_services|group_boundary|retail|建筑和构件供应具有商品供应属性，原 office_services 不足以表达销售；需按供货销售与配套服务的主要功能核对，不影响 include。|
|02100180|TATTOOING AND PIERCING SERVICES|retail|clear_conflict|office_services|纹身与穿孔以提供个人服务为主，不能因有门店而归 retail；本轮按要求单列，暂保留原组。|
|02100182|TROPHIES AND ENGRAVING SERVICES|retail|group_boundary|retail / office_services|类别同时涵盖奖杯商品供应和雕刻定制服务，均应 include；以商品销售还是定制服务为主关系到功能组，现有类别不宜强行拆分。|
|02100183|VEHICLE CLEANING SERVICES|retail|clear_conflict|office_services|洗车提供清洗服务，应与车辆商品销售区分；原 retail 单列修正建议，暂保留。|
|02100776|TAILORING AND CLOTHING ALTERATION|retail|clear_conflict|office_services|裁缝和衣物修改主要提供定制修整服务，建议由 retail 改为 office_services，本轮未擅自改组。|
|02100826|PRINTING ON GARMENTS|retail|clear_conflict|office_services|服装印花提供定制加工服务，不因交付衣物就按零售，建议 office_services。|
|02130206|HOUSEHOLD REPAIRS AND RESTORATION|retail|clear_conflict|office_services|家居维修修复以维修服务为主，原 retail 与明确的零售/服务边界冲突。|
|02130210|SPORTS AND LEISURE EQUIPMENT REPAIR|retail|clear_conflict|office_services|体育休闲器材维修提供维修服务，应与器材销售区分，建议 office_services。|
|02130212|VEHICLE REPAIR, TESTING AND SERVICING|retail|clear_conflict|office_services|车辆维修、检测和保养提供服务，不能仅因有接待门店归 retail。|
|02130793|SHOE REPAIRS|retail|clear_conflict|office_services|修鞋是生活维修服务，原 retail 与用户规定的服务边界冲突。|
|02150218|AIRLINES AND AIRLINE SERVICES|office_services|group_boundary|transport_parking|航空公司及航空服务以运输运营为核心，原 office_services 是否仅表达办公室需核对；include 已确定。|
|02150224|FERRY AND CRUISE COMPANIES|office_services|group_boundary|transport_parking / leisure_tourism|渡轮与邮轮公司明确具有运输或旅游运营功能，原 office_services 需核对；仅功能组边界不形成 review。|
|02150227|RAILWAY RELATED SERVICES|office_services|group_boundary|office_services / transport_parking / industry_logistics|铁路相关服务的运营、商务支持和物流均在纳入范围内，需核对分组侧重而不是排除类别。|
|02600097|BOAT HIRING SERVICES|office_services|group_boundary|office_services / transport_parking / leisure_tourism|船舶租赁有租赁服务、交通与游憩属性，原组保留；归类边界不改变 include。|
|05260316|ANIMAL CLIPPING AND GROOMING|retail|clear_conflict|office_services|宠物修剪美容以生活服务为主，应与宠物或用品销售区分，原 retail 单列修正建议。|
|06340802|WIFI HOTSPOTS|（空）|unassigned_included|office_services / education_health_public|WiFi 热点的上网服务功能明确，include；通信服务与公共便民服务两种分组边界待核对，v2 保留原空组而不把该行变为 review。|
|09480691|PETS, SUPPLIES AND SERVICES|retail|group_boundary|retail / office_services|宠物、用品和服务混合了销售与生活服务，均应纳入；原 retail 暂保留，不能因功能组不唯一排除。|
|09480694|TRAVEL AGENCIES|retail|clear_conflict|office_services|旅行社以旅行咨询预订服务为主，用户标准明确属于办公与服务；按本次交付要求单列原 retail 冲突而未擅自扩展明确改组清单。|
|10560753|MOORINGS AND UNLOADING FACILITIES|transport_parking|group_boundary|transport_parking / industry_logistics|系泊与装卸都是明确服务，新增纳入暂归 transport_parking；货运装卸也可归物流，分组侧重需核对但不影响 include。|

WiFi 热点 06340802 保持 include，功能组暂空；本轮标准允许纳入与归组分开。此处不使用 review 代替归组不明，也没有新增 Other 组。

## 同类标准一致性复查

1. ATM、PayPoint、饮水点、邮筒、公厕、WiFi 和邮局全部 include。设备、无人值守、附属于商店或银行均不造成整类排除；共址不等于重复。
2. 公交站、招手即停区域、轨道站、地铁入口、机场/起降场、直升机坪、公交运营场站均 include。10570738 保留 review 的原因是含网络交汇点，不是车站不公开或入口可能重复。
3. 工厂、矿山、农场、能源生产、水泵站、羊只药浴清洗和地磅保留。工业产品类的设备名称按制造业上下文解释为生产供应，不按已安装网络构件排除；桥梁、管线、信号设备等纯构件排除。船闸作为执行通航转换的明确运行设施纳入；渡槽、堰/泄水闸/坝本类表示工程结构，不替代泵站、发电场所或直接服务设施。02030047 的排除来自本次明确决定，不外推到租赁或生产类别。
4. 公园、文化遗址、鸟类保护场所、公地及其他旅游景点 include，不要求收费或管理证明。湖泊等水体和测量点 exclude；水库位于水体子类，按水体本身处理，不推断为供水厂。指定风景要素是否仅地理标记仍 review。
5. 10540832 exclude；同址的酒店、购物中心、停车场、加油站及高速服务区均按各自功能 include。此表没有位置字段，未识别或统计实际共址、本站/邻站充电设施。
6. 以商品销售为主与以服务为主分别理解。明确指定的理发美容、开锁和个性化定制已改组；其他明显 retail/服务冲突单列，未因此降低纳入资格。

## 使用边界与尚未执行

这是一份类别规则审查交付，未改动 src/、tests/、v1 CSV/YAML 或运行入口，未生成 v2 YAML，也未启动 POI 点位清洗、全源分析、空间匹配、城市组成或邻近充电设施指标。既有 loader 要求 include 必须有组，且仍绑定 v1；因此本次含 WiFi 空组的审查表不能直接替换运行配置。正式接入应另行处理这一接口约束及已标出的功能组冲突，并验证新的点位结果。

未知实际 POI 分类码的处理不在这份已列类别表审查范围内；不据本表推断真实点位是否重复、过时或定位错误。同站多个入口及同机构重复记录应在记录阶段统一计数，但不会仅按同址合并商店/PayPoint、银行/ATM等不同服务。场所与小型服务点均可纳入，POI 个数不代表面积、就业规模或客流。

保留官方 Group/Category/Class 身份列，主要组成继续遵循此前采用官方 Group 的口径；本轮按要求保留并修正审查表中的 poi_function_group，不重新设计分组，也不据此重新启用八功能组作为主统计分类。类别纳入与统计分组是两件事，最新 prompt 取代此前“取消全部类别筛选”的待实施方案。本轮结果仍待用户检查，未提交 Git。

审查辅助脚本、显式逐行决定表及复核证据保存于 `/mnt/data/work/mazhichuan/analysis_Digimap_POI_v2/outputs/step11-rules-v2-review-20261012T080208Z`。
