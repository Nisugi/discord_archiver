import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv()

TOKEN         = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set in .env file")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env file")

SOURCE_GUILD_ID     = 226045346399256576   # GemStone IV
AGGREGATOR_GUILD_ID = 1383182313210511472  # Not used in archive mode
CENTRAL_CHAN_ID     = 1383196587270078515  # Not used in archive mode

# Crawler settings
# Crawl roughly 12 hours (0.5 days) of history when the bot starts
CRAWL_BACKFILL_DAYS = 3       # How far back to crawl on startup
REQ_PAUSE = 1.5                 # Seconds between Discord API requests
PAGE_SIZE = 100                 # Messages per page when crawling
CRAWL_VERBOSITY = 10           # Print progress every N saves
COMMIT_BATCH_SIZE = 10         # Commit to DB every N messages

# Repost settings
REPOST_DELAY_SECONDS = 300     # 5 minutes delay before reposting
CREATE_COOLDOWN = 1            # Seconds between creating channels/threads
API_PAUSE = 2.1                # Pause between webhook sends

# Use a different database name for the full archive
DB_PATH = Path("/data/discord_archive.db")
# DB_PATH = Path("./discord_archive.db")
GDRIVE_URL = "1zZkMejDgk28R9IzqvCkn4H9LARcetfcg"

# Performance settings for large database
VIEWER_PAGE_SIZE = 25           # Results per page in web interface
VIEWER_MAX_RESULTS = 10000      # Max total results to prevent memory issues
AUTO_VACUUM_THRESHOLD = 1000000 # Auto-vacuum after 1M operations
BACKUP_FREQUENCY_HOURS = 6      # Backup frequency (if using GitHub backup)
ENABLE_QUERY_TIMING = True      # Log slow queries
SLOW_QUERY_THRESHOLD = 1.0      # Log queries taking >1 second

GM_NAME_OVERRIDES = {}  # Not needed for archive mode

# Channel filtering configuration
# Channels we can access but want to hide from public view
PRIVATE_CHANNELS = {
    613879283038814228,  # Off-Topic
    1333880748461260921, # Platinum off-topic thread
    1171221232402845767, # Games and Trivia
}

# Channels we don't have access to (will be auto-detected and cached)
# Listed here for documentation purposes
INACCESSIBLE_CHANNELS = {
    288345077422620672,  # sgm
    1017239033295937626, # mentors-doorbell
    778411011182034974,  # gm-game-night-text
    308092379305476098,  # dev-social
    728703927540908042,  # gsl-class
    309145507136143360,  # gsl-editor
    1288604440273883209, # operations
    988880338656833606,  # paid-events
    605483405647413260,  # gm-responses
    961341625232142376,  # gm-wiki
    1209589829843820615, # gsl-mentorship-2024
    352811786698752001,  # gsl
    481822105323700255,  # new-player-experience
    1105587083034243203, # simucon-attendees
    309033034672242698,  # bottest
    746065960569536572,  # mods-gms
    822226441432858665,  # discord-auth
    308640359377010698,  # platinum
    728291124589887621,  # esp
    890363842394193950,  # creatures
    988880392939524096,  # world-events
    309126438311690240,  # simucon
    587438372239310848,  # nitro-boosters
    1225268403544002641, # all-hands
    555594922422435840,  # gm-social
    893632766871207946,  # new-player
    690724412705734666,  # gm-backchannel
    823885203931922522,  # mentor-social
    1223702130431365150, # wiki-cleanup
    1238218574095581214, # systems
    727536435762167848,  # qc-cleared
    783076106453516299,  # rumor-woods
    783075975076118561,  # ebon-gate
    1309673921607635009, # platinum-migration
    311353643771363330,  # script-monitor
    961341808988811274,  # wiki-wranglers
    988880433955606588,  # storylines
    794056415257165825,  # community-alerts
    676844435505676319,  # game-alerts
    1120449094658310244, # dev
    1120449500377530428, # discord-mod
    288343253105770506,  # mentors
    288344594838716416,  # gm
    288343360329220097,  # mentors-council
    730217090207973396,  # production
    288345179025571841,  # gamehost
    308092168168538112,  # dev
    716717239083991131,  # testing
    988880970990108763,  # premium-team
    499637538198257684,  # mod-log
    227431274514612224,  # lnet
    776886728404631552,  # discord-policy
    1275507286294528134, # bugs
    783075908277633084,  # duskruin
    1240407380123062374, # inquisitor
    1011702603190644796, # production-meeting
    308092728607113216,  # training
    988879531945394186,  # player-experience
    1212818059061100544, # gsl-mentors
    902666856891035678,  # events
    980911407992295434,  # apm
    1036813094770458695, # mod-edit-delete-log
    511297034284957696,  # storylines
    1001657677161709578, # dev meetings
    776573543756464129,  # ongoing training
    1055009492607193159, # prime
}

# Forums to skip during crawling due to volume (but still process new messages)
SKIP_CRAWL_FORUMS = {
    # Add forum channel IDs here that have thousands of threads
    # These will be skipped during backfill but new messages will still be processed
}

# Featured channels - curated list shown by default in channel dropdown
# Maps channel ID to display name (None = use Discord's name)
# This is the single source of truth for featured channels and their display names
FEATURED_CHANNELS = {
    387270949499830273: "game-chatter",  # game-chatter
    1026549641963126795: "general forum",  # general forum (override to distinguish from general channel)
    387271714012135425: "help",  # help channel
    1026551028788772914: "help forum",  # help forum (override to distinguish from help channel)
    387271313695309824: "prime",  # prime
    387271364572086272: "premium",  # premium
    387271396218241024: "platinum",  # platinum
    387271438081720331: "shattered",  # shattered
    387271681539833858: "festivals-and-events",  # festivals-and-events
    541653031691747329: "roleplaying",  # roleplaying
    387286877327065108: "scripting",  # scripting
    1082473495214182460: "simucon",  # simucon
    1331043359715758162: "merchanting",  # merchanting
    532373273816858636: "mechanics",  # mechanics
    1026551770299768925: "mechanics forum",  # mechanics forum
    1271943281613340775: "gemstones",  # gemstones
    930939030546296832: "bards",  # bards
    930938941534789672: "clerics",  # clerics
    930938962808274975: "empaths", # empaths
    930939047700992080: "monks", # monks
    930939059268890644: "paladins", # paladins
    930939014956073011: "rangers", # rangers
    930938808709578822: "rogues", # rogues
    930938987814744115: "sorcerers", # sorcerers
    930938713800867952: "warriors", # warriors
    930938913613295707: "wizards", # wizards
    1026553026326056991: "professions forum",  # professions forum
    928769266008879104: "towns",  # towns
    1026562326461161483: "towns forum",  # towns forum
    928780797899595896: "cysaegir",  # cysaegir
    928780617913610240: "icemule-trace",  # icemule-trace
    928780402154434580: "kharam-dzu-teras",  # kharam-dzu-teras
    928780821249294387: "kraken's-fall",  # kraken's-fall
    928780775380353054: "mist-harbor-four-winds",  # mist-harbor-four-winds
    928780592147996702: "river's-rest",  # river's rest
    1255994886621106287: "sablecross",  # sablecross
    928780559373705256: "solhaven",  # solhaven
    928780701426397266: "ta'illistim",  # ta'illistim
    928780734079070218: "ta'vaalor", # ta'vaalor
    928780323339272222: "wehnimer's-landing",  # wehnimer's-landing
    928780650859864084: "zul-logoth",  # zul-logoth
    1026556174188937236: "world-events forum",  # world-events
    1027573075648200734: "che-mho forum",  # che-mho
    1026553470192459866: "races forum",  # races
    1026554655565680792: "roleplaying forum", # roleplaying
    594010837732294735: "events",  # events
    594009933763051549: "duskruin",  # duskruin
    594009960166457344: "ebon-gate",  # ebon-gate
    701166110204231733: "rings-of-lumnis",  # rings-of-lumnis
    594009994953883648: "rumor-woods",  # rumor-woods
    1026555304550355044: "paid-events forum",  # paid-events forum
    988879927824777216: "archive",  # archive
}

# Automatically derived from FEATURED_CHANNELS
FEATURED_CHANNEL_IDS = set(FEATURED_CHANNELS.keys())

# Channel name overrides - derived from FEATURED_CHANNELS
CHANNEL_NAME_OVERRIDES = {k: v for k, v in FEATURED_CHANNELS.items() if v is not None}

# GM name overrides - maps Discord user ID to display name
# This is the single source of truth for all GMs
GM_NAME_OVERRIDES = {
    928613234263605298: "Naijin 2.0",          # spiffyjr
    84034005221019648: "Naijin",           # spiffyjr
    111937766157291520: "Estild",          # glyph.dev  
    308821099863605249: "Wyrom",           # Keep as Wyrom
    316371182146420746: "Isten",           # 
    310436686893023232: "Thandiwe",        #
    388553211218493451: "Tivvy",           #
    105139678088278016: "Auchand",         #
    75093792939581440: "Mestys",           #
    312977191933575168: "Vanah",           #
    287728993107443714: "Elysani",         #
    716406583248289873: "Xynwen",          #
    205777222102024192: "Haxus",           #
    436340983718739969: "Naiken",          #
    287266173673013251: "Naionna",         #
    287057798955794433: "Valyrka",         #
    1195153296235712565: "Weaves",         #
    710276421003640862: "Yusri",           #
    557733619175653386: "Meraki",          #
    413715970511863808: "Avaluka",         #
    898650991195463721: "Casil",           #
    1182779174029635724: "Eusah",          #
    312280391493091332: "Flannihan",       #
    560411563895422977: "Itzel",           #
    135457963807735808: "Scrimge",         #
    321823595107975168: "Sindin",          #
    562749776026664960: "Xeraphina",       #
    307156013637828619: "Elidi",           #
    913160493965922345: "Ethereal",        #
    908492399376998460: "Marstreforn",     #
    1195134155521020026: "Optheria",       #
    1190437489194844160: "Aergo",          #
    1195603135268405309: "Azidaer",        #
    711671094003630110: "Gyres",           #
    557733716538163201: "Ivry",            #
    1181709242487558144: "Kaonashi",       #
    235241271751344128: "Lydil",           #
    370113695201886210: "Mariath",         #
    1195186424513839114: "Nyxus",          #
    1083646594823491605: "Tago",           #
    1200407603797303359: "Warlockes",      #
    294990044668624897: "Zissu",           #
    306987975932248065: "Retser",          #
    200287510088253440: "Naos",            #
    307031927192551424: "Coase",           #
    426755949701890050: "Quillic",         #
    299691771657715712: "Xayle",           #
    308625197852917760: "Ixix",            #
    113793819929083905: "Konacon",         #
    1195131331047346246: "Apraxis",        #
    190295595125047296: "Tamuz",           #
    306995432981266433: "Modrian",         #
    401257353866903553: "Kenstrom",        #
    226067036768305153: "Haliste",         #
 	287013494308601857: "Kynlee",          #
 	287043282939412482: "Zoelle",          #
    278554111232704514: "Mazreth",         #
    307218082798108672: "Galene",          #
    308769643890475010: "Viduus",          #
    912540641521709138: "Wylloh",          #
    168526412561514496: "Sleken",          #
    454442580789428224: "Lanadriel",       #
    728119544752635987: "Khorbin",         #
    475856821085667329: "Ubiq",            #
    238006234077200385: "Kaikala",         #
    905666478546780160: "Reidyn",          #
	321708823490592778: "Jainna",          #
	307170903563698176: "Kveta",           #
    869737767972773928: "Hivala",          #
	454105842971836427: "Netz",            #
    423659565167411204: "Annanasi",        #
    307164703652708352: "Contemplar",      #
    238436170999005184: "Skhorne",         #
    381845577987653632: "Sotsona",         #
    306983375103590401: "Palvella",        #
 	452264876006703115: "Wraex",           #
    309134402732949506: "Cyraex",          #
    282728416556613643: "Necios",          #
    309864637334028288: "Aulis",           #
}

# Automatically derive SEED_BLUE_IDS from GM_NAME_OVERRIDES
# This eliminates duplication and ensures they stay in sync
SEED_BLUE_IDS = set(GM_NAME_OVERRIDES.keys())

# Legacy - for backward compatibility
IGNORED_CHANNELS = PRIVATE_CHANNELS | INACCESSIBLE_CHANNELS

# Deprecated settings (kept for reference but not used)
ARCHIVE_MODE = False            # No longer needed after initial setup
CUTOFF_DAYS = CRAWL_BACKFILL_DAYS  # Alias for compatibility


# Unable to find discord user for:
# Donagn
# Zythica
# Liia
# Wakefield
# Finros
# Keios
# Draxun
# Mikos
# Nebhrail
# Rhameis
# Serannyse
# Oscuro    found but deleted
# Deleted User — 5/2/2020 9:34 AM
# This was a premature deployment of the NoAmbientMsg flag.  Please do not test it yet.  You can safely wait for an announcement when details will be given.
#
# Discord assigns deleted users the same user id, so no way to distinguish posts between deleted users.