# chunk_md.py
import os
import re
import json
import argparse
import bisect
import time
import sys
from html import unescape
from html.parser import HTMLParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build_chunk_common_fields(
    *,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    return {
        "source_type": source_type,
        "file_format": file_format,
        "chunk_type": chunk_type,
        "source_record_type": source_record_type,
        "source_record_id": source_record_id,
    }


def save_json(data, file_path):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_single_file(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    if ext != '.md':
        raise ValueError(f"Unsupported file extension: {ext}; only .md is supported")

    print(f"Reading file: {file_path} ...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='gbk') as f:
            content = f.read()

    file_name = os.path.splitext(os.path.basename(file_path))[0]
    return file_name, content

def extract_image_paths(text):
    """
    闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁炬儳缍婇弻鐔兼⒒鐎靛壊妲紒鎯у⒔閹虫捇鈥旈崘顏佸亾閿濆簼绨奸柟鐧哥秮閺岋綁顢橀悙鎼闂傚洤顦甸弻銊モ攽閸♀晜效婵炲瓨鍤庨崐婵嬪蓟閵堝绾ч柟绋块娴犳挳鎮楀▓鍨灈闁绘牜鍘ч悾鐑芥偂鎼搭喗鍍靛銈嗘尵閸犳捁銇愰崨瀛樷拻濞达綀顫夐崑鐘绘煕閺傝法肖闁瑰箍鍨藉畷姗€顢欓崲澶涚畵閺屾盯寮撮妸銉т哗闂佸憡鍔忛崑鎾翠繆閻愵亜鈧牠宕濋敃鈧…鍧楀焵椤掑倻纾兼い鏃傚帶椤ｅ磭绱掓潏銊﹀鞍闁瑰嘲鎳橀獮鎾诲箳瀹ュ拋妫滃┑鐘垫暩婵即宕规總绋挎槬闁哄稁鍘肩粈澶愭煛瀹ュ骸骞楅柛瀣€圭换娑㈠箣濞嗗繒浠鹃梺缁樻尰濞茬喖寮婚弴銏犻唶婵犻潧娲ゅ▍褔姊虹化鏇熸珨缂佹煡绠栨俊鐢稿礋椤栨稒娅滈棅顐㈡处濞叉粓寮抽悩缁樼厸濞达絽鎽滄晶娑樓庨崶褝韬┑鈥崇埣瀹曘劑顢欓崗纰变画闂傚倷鐒︽繛濠囧绩闁秴鍨傞柛褎顨呴拑鐔兼煟閺傚灝鎮戦柛銈呭暣閺屽秵娼悧鍫▊缂備緡鍠栭悥鐓庮潖濞差亜宸濆┑鐘插暊閹峰綊姊洪悷鐗堝暈濠电偛锕ら锝囨嫚濞村顫嶉梺闈涚箳婵兘顢欓幒妤佺厽閹兼番鍔嶅☉褔鏌熺拠褏绡€鐎规洖鐤囬ˇ褰掓煛鐏炵喎娲﹂崵鎺楁煏閸繃鍣告繛宀婁邯濮婃椽鏌呭☉姘ｆ晙闂佸憡鏌ㄩ惌鍌炲灳閺冨牆绀冩い蹇撴噹閻濅即姊绘繝搴′航闁告ê銈歌棢婵鍩栭埛鎺楁煕鐏炲墽鎳呮い锔肩畵閺岀喓鍠婇崡鐐板枈閻庢鍠氶弫濠氥€佸Δ鍛妞ゆ巻鍋撻柛鎿冨枛椤啴濡堕崱娆忣潷缂備礁顑嗛崝妤呭箲閵忋倕骞㈡繛鎴炵懅閸樹粙姊虹涵鍛涧缂佹彃鈧噥鏆遍梻鍌欑閹碱偊骞婅箛娑欏亗闁跨喓濮撮拑鐔哥箾閹寸們姘ｉ崼銉︾厱婵°倕鍟禒婊堟倵濞戝磭绉慨濠呮閹风娀骞撻幒婵嗗Ψ闂備礁婀遍…鍫ュ疮閸ф缍栭煫鍥ㄦ礈绾惧吋淇婇婵愬殭妞ゅ孩鎹囧铏圭磼濡櫣浠搁梺鎸庣缁绘盯宕煎┑鍫㈠姱濠殿喖锕ュ浠嬬嵁閺嶎厽鍊烽柤纰卞厸閻ヮ亪鏌ｉ悢鍝ョ煁婵犮垺锕㈠畷顖炲箻椤旇偐鐣烘俊銈忕到閸燁垶宕愰懡銈囩＜婵炴垶锕╅崕鎰版煛閸涱偄鐏叉慨濠冩そ瀹曘劍绻濋崘顭戞П闂備胶顭堥鍛涘┑鍡欐殾闁挎繂顦悞鍨亜閹哄棗浜鹃梺瀹狀潐閸ㄥ潡骞冮埡鍜佹晩闁兼祴鏅欑槐姗€姊绘担铏广€婇柡鍛矒閹囨偐閼碱剚娈惧┑鐘绘涧椤戝懘宕￠幎鑺ョ厽婵☆垰鎼幃浣虹磼濡も偓椤︽壆鎹㈠┑瀣棃婵炴垶菤閸嬫捇骞栨担鍝ョ枀闂佽法鍠撴慨鐢稿磻閸岀偞鐓涢柛銉ｅ劚閻忣亪鏌ｉ幘瀵告噰闁哄瞼鍠愰幏鍛村礃閳哄啫娑ф繝鐢靛仜閹冲酣骞婃惔銊ョ厴闁硅揪闄勯鎰版⒑缁嬫鍎愰柛鏃€顨呭嵄闁圭増婢樼粻铏繆閵堝嫮顦﹀ù婊冪秺濮婂宕掑鍗烆杸缂備礁顑嗛崹鑸电┍婵犲洦鎯為柛锔诲幘閿涙繃绻涙潏鍓хК婵炲拑绲块弫顔尖槈濮樿京锛滈柣鐘叉穿鐏忔瑦鏅堕敂閿亾鐟欏嫭绀€闁圭⒈鍋婇崺銉﹀緞閹邦剦娼婇梺缁橈耿濞佳勬叏閿旀垝绻嗛柣鎰典簻閳ь剚鐗滈弫顕€骞掗弬鍝勪壕婵鍘ф晶鎵磼椤旇偐澧︾€规洘锕㈤崺鐐村緞閸濄儳娉块梻浣烘嚀閸氬骞嗗畝鍕獥闁哄稁鍘介崐闈涒攽閻樺磭顣查柣鎾存礃缁绘盯骞嬪┑鍡欑М缂備降鍔忛褔婀?Markdown 闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁炬儳缍婇弻鐔兼⒒鐎靛壊妲紒鐐劤缂嶅﹪寮婚敐澶婄闁挎繂鎲涢幘缁樼厱濠电姴鍊归崑銉╂煛鐏炶濮傜€殿喗鎸抽幃娆徝圭€ｎ亙澹曢梺鍛婄缚閸庤櫕绋夊澶嬬厸鐎广儱楠搁獮妤呮煟閹惧瓨绀冮柕鍥у楠炲洭宕滄担鑽锋垹绱撴担鎻掍壕闂侀€炲苯澧扮紒杈ㄥ浮閹瑩顢楅埀顒勫礉閵堝棛绠鹃悘蹇旂墤閸嬫捇骞囨担鍛婎吙闂備礁澹婇崑鍛洪弽顓熺厑闁搞儯鍔庣粻楣冩煙鐎甸晲绱虫い蹇撶墐閳ь剚鐗楀鍕箾閻愵剚鏉搁梻浣虹帛閸旀洖顕ｉ崼鏇為棷闁芥ê顦弨浠嬫煟閹般劍娅呭ù婊堢畺濮婄粯鎷呴崨濠冨創闁荤偞鍑归崑濠傜暦閹邦兘鏀介悗锝庡墮缁侊附绻涢幘鏉戠劰闁稿鎸婚〃銉╂倷閺夋垶璇炲Δ鐘靛仜椤戝懘鍩為幋锕€骞㈤柍鍝勫€圭粭搴♀攽閻樺灚鏆╁┑顔惧厴瀵偊骞栨担鍝ワ紱濠电偞鍨崹鍦不閻樼粯鐓欓梺顓ㄧ畱楠炴绱掗悩鑽ょ暫闁哄苯绉烽¨渚€鏌涢幘璺烘灈鐎殿喖顭烽弫宥夊礋椤忓懎濯扮紓鍌欑贰閸ㄥ崬煤閺嶃劎顩插Δ锝呭暞閻撴瑥螞妫颁浇鍏屾い锔肩畵閺岀喖顢欓挊澶屼紝闂佸搫鐬奸崰鏍箠閺嶎厼鐓涢柛鏇烇工椤︾敻寮诲鍫闂佸憡鎸荤换鍕缁嬪簱鏋庨柟鎯х－椤︹晠鎮峰鍛暭閻㈩垱顨婇幃锟犳偄閻撳海顔愬┑鐑囩秵閸撴瑩鍩€椤戞儳鈧洟鈥﹂崶顒€绠涙い鎾跺Х椤旀洟姊洪崨濠勬噧妞わ箒椴搁弲鍫曟偨閸涘﹤鐧勫┑鐘绘涧閺嬬銇愰幒鎴狀槯闂佺绻楅崑鎰矙閸ャ劋绻嗘俊銈傚亾闁硅櫕锚椤繐煤椤忓嫬绐涙繝鐢靛Т閸燁偊藝閳哄懏鈷戦柟鑲╁仜婵℃椽鏌涘Δ鈧崯鍧楁偩瀹勯偊娼ㄩ柍褜鍓熷畷娲焵椤掍降浜滈柟鐑樺灥椤忣亪鏌嶉柨瀣伌婵﹤顭峰畷濂告偄閸撲胶绠掗梻浣虹帛閹尖晠宕㈡總绋跨厴闁硅揪绠戦悘鎶芥煣韫囷絽浜濋柟顔界懇濮婂搫鐣烽崶鈺佺闂佸湱鎳撳ú锔界┍婵犲洤閱囬柡鍥╁仧閸婄偤姊洪崘鍙夋儓闁哥姵绋撳Σ鎰板礃濞村鏂€闂佺粯鍔橀崺鏍亹瑜忕槐鎺楀箵閹烘挸浠村Δ鐘靛仜閿曨亪鐛Ο灏栧亾濞戞顏勵嚕閹稿海绡€闁靛骏绲介悡鎰版煕閺冣偓閻楃娀骞冮敓鐘插嵆闁靛繆鈧枼鍋撻悽鍛婄叆婵犻潧妫楅弳娆忊攽閳ョ偨鍋㈤柡灞界Х椤т線鏌涢幘瀛樼殤闁瑰箍鍨藉畷鎺楁倻閸℃ɑ娅旈梻浣瑰缁诲倸螞濞戞艾濮柍褜鍓欓埞鎴︻敊閺傘倓绶甸梺绋挎捣閺佸宕洪敓鐘插窛妞ゆ梹鍎崇敮鎯р攽閻橆喖鐏辨繛澶嬬洴閹囧幢濞戞瑥鈧埖銇勮箛鎾村櫡濞存粍绮嶉妵鍕箛閳轰胶浼勯悗娑欑箞濮婅櫣鈧湱濯鎰版煕閵娿儲鍋ョ€规洘妞芥俊鐑藉煛娴ｆ瓕鈧灝鈹戞幊閸婃洟宕埡浼瑰酣顢氶埀顒€顫忕紒妯诲濞撴凹鍨抽崝绋款渻閵堝棗鐏ユ俊顐ｇ箓閻ｅ嘲鈹戦崱蹇旂€婚棅顐㈡处濞叉ê鈻撻幆褉鏀介柣妯肩帛濞懷勩亜閹寸偛濮嶇€殿噮鍋呯换婵嬪炊閵娧冨箞闂備礁婀遍崑鎾汇€冮崨鏉戠闁瑰濮甸～鏇㈡煕閹邦厾銈撮柡鈧禒瀣厽闁归偊鍨伴悡鎰喐閹跺﹤鎳愮壕鐣屸偓骞垮劙缁€浣圭閻愵剛绡€闁汇垽娼ф禒婊堟煙闁垮鐏╃€垫澘锕畷绋课旈埀顒勫几娴ｅ箍浜滈柡鍌濇硶濮ｇ偞淇婇幓鎺斿ⅵ闁哄本娲濈粻娑㈠Ψ瑜忛敍鐔兼⒑閸涘鑰垮ù婊嗘硾椤繐煤椤忓拋妫冨┑鐐村灱娴滎剟宕濋幖浣光拺缂佸瀵ч崬澶嬬箾閸涱喗绀堢紒顔碱儔楠炴帒螖閳ь剟锝為崨瀛樼厪闁割偅绻冮ˉ鐘绘煕濡湱鐭欐慨濠冩そ楠炴劖鎯旈敐鍥╂殼婵犵數鍋涢惇浼村磹濠靛棛鏆﹂柕蹇婃噰閸嬫捇鎮介悽鐐光偓濠囨煕鐎ｎ偅灏甸柟鍙夋尦瀹曠喖顢楅崒銈喰氶梻鍌欑窔濞佳囨偋韫囨稈鈧箓宕奸妷顔芥櫔闂佹寧绻傞ˇ顖滅不缂佹ǜ浜滈柡鍐ㄦ处椤ュ霉濠婂嫮鐭掗柡宀嬬秬缁犳盯骞橀崜渚囧悈缂傚倷璁查崑鎾趁归敐鍛础妞も晜褰冭灃闁挎繂鎳庨弳鐐烘煟閹捐泛鏋涢柡宀€鍠愬蹇涘礈瑜嶉崺宀勬偠濮橆厼鍝烘慨濠呮缁辨帒螣鐠囨煡鐎洪梻浣藉吹閸熸瑩宕ㄩ鍛稐闂備浇顫夐崕鎶芥偤閵婏箑鍨旈柡澶嬪灍閺€鑺ャ亜閺冨洦顥夐柣鎺撳劤闇夋繝濠傛噹娴犙囨煕閹烘挸娴い銏★耿閸┿儵宕卞Ο鐐╂櫊濮婂宕掑顓熸倷濡炪倧濡囬弫璇差嚕婵犳碍鏅查柛娑樺€婚崰鏍х暦椤愶箑绀嬫い鎺嶈兌濡差亪姊婚崒娆戭槮闁硅绱曢幑銏ゅ礃椤垶瀵屾繛瀵稿Т椤戝懘鎷戦悢鍏肩厓闁靛鍎抽敍宥夋煛閸℃顥㈤柟顔筋殜閺佹劖鎯旈垾鎰佹骄闁荤偞鐔粻鎾愁潖缂佹ɑ濯寸紒娑橆儏濞堟劙姊洪崫銉バｆい銊ワ躬楠炲啴鎮块锝嗏枌闂備胶纭堕弬渚€宕戦幘鎰佹富闁靛牆妫楃粭鎺楁煕婵犲倻绉虹€殿喗濞婇崺锟犲礃椤忓拑绱℃俊鐐€栭幐鎾礈濠靛牊鍏滃Δ锝呭暞閹虫岸鏌ｉ幇顔煎妺闁绘挸绻愰埞鎴︽倷闂堟稐澹曞┑鐐叉噹濡繈寮诲澶嬬叆閻庯綆浜炴导宀勬⒑閸濆嫭婀扮紒瀣灴閸┿儲寰勬繝搴㈠缓闂佸壊鍋嗛崰鎰板汲娴煎瓨鈷掑ù锝堟鐢盯鏌熺粙璺ㄥ煟鐎规洘娲熼幃鐣岀矙閼愁垱鎲伴梻渚€娼ц墝闁哄懏鐩畷锟犲箮閼恒儳鍘繝鐢靛仜閻忔繈鍩€椤掍胶绠炵€规洘鍨佃灃闁告侗鍠栭埀顒傛暬閹嘲鈻庤箛鎿冧痪缂備讲鍋撻柛鎰ㄦ櫃缁诲棝鏌ｉ幇顓烆棆闁活厽鐟ч埀顒冾潐濞叉ê顪冩禒瀣槬闁?
    闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁炬儳缍婇弻鐔兼⒒鐎靛壊妲紒鎯у⒔閹虫捇鈥旈崘顏佸亾閿濆簼绨奸柟鐧哥秮閺岋綁顢橀悙鎼闂侀潧妫欑敮鎺楋綖濠靛鏅查柛娑卞墮椤ユ艾鈹戞幊閸婃鎱ㄩ悜钘夌；婵炴垟鎳為崶顒佸仺缂佸瀵ч悗顒勬⒑閻熸澘鈷旂紒顕呭灦瀹曟垿骞囬悧鍫㈠幈闂佸綊鍋婇崹鎵閿斿墽纾介柛鎰ㄦ櫆缁€瀣叏婵犲嫮甯涢柟宄版嚇瀹曘劍绻濋崘銊ュ濠电姷鏁搁崑娑㈡儑娴兼潙绀夌€广儱顦粻鐐烘煏婵犲繐鐦滄俊鎻掔秺楠炴牗娼忛崜褎鍋ч梺缁樺笒閸氬鎹㈠┑瀣仺闂傚牊鍒€閵忋倖鐓曞┑鐘插閺嗩剛鈧鍠撻崝宥囩矉閹烘柡鍋撻敐搴′簽闁告妫勯埞鎴﹀煡閸℃浠╅梺鍦拡閸嬪棝鎯€椤忓浂妯勯梺鍝勭灱閸犳牕鐣峰Δ鍛亗閹肩补妲呭姘舵⒒娴ｅ憡鎯堥柡鍫墰缁瑩骞樼€靛壊娴勯梺鎸庢⒒閸嬫挾鈧碍宀搁弻鐔虹磼濡桨鍒婂┑鐐靛帶闁帮絽顫忕紒妯诲闁告稑锕ラ崕鎾绘⒑閸濆嫮澧遍柛鎾寸懅閸欏懘姊虹紒妯活棃妞ゃ儲鎹囧鎶芥晝閸屾稓鍘介梺瑙勫劤绾绢厼鐣濋幖浣圭厸闁逞屽墯缁傛帞鈧綆鍋嗛崢鐢告⒒娓氬洤寮跨紒鐘冲灩閺侇噣濡搁埡鍌滃弳闂佸搫娲ㄩ崑娑㈠焵椤掆偓缂嶅﹪宕哄☉銏犵闁挎梻鏅崢鍗炩攽閻愭潙鐏﹂柨鏇ㄥ亜椤斿繘濡烽妷銏℃杸濡炪倖姊婚幊鎾寸妤ｅ啯鈷掗柛灞剧懆閸忓本銇勯鐐靛ⅵ妞ゃ垺鐗犲畷鍗炩槈濡⒈鍞归梻浣规偠閸庢粎浠﹂崜褏鈻夌紓鍌氬€搁崐鐑芥倿閿曚焦鎳屽┑鐘愁問閸ㄩ亶骞愰幎钘夎摕鐟滄垹绮诲☉銏犲嵆闁绘棃鏀遍悾顒傜磽閸屾瑧鍔嶆い銊﹀姉閹广垹鈹戦崱鈺傜稁濠电偛妯婃禍婵嬪磻閿熺姵鐓熸俊銈傚亾闁绘锕畷浼村箛椤掑瀵岄梺闈涚墕妤犳悂鐛弽顓熺厽婵°倓鐒︾亸顓㈡煥閺囨ê鐏柟宄版嚇閺屽懎鈽夊杈╂毎闂傚倷鑳剁划顖炲礉閺囩倣鐔哥節閸パ咃紵闂婎偄娲︾粙鎺楀磹閸偆绠鹃柟瀵稿仜缁楁碍绻涢崨顓ㄥ姛闁逞屽墲椤煤濮椻偓閵嗗啴宕ㄧ划鍏夊亾閿曞倸惟闁宠桨绶氶崬鍫曟⒑缂佹ɑ鐓ラ柟纰卞亞閺? ![alt](path) 闂?![alt](path "title")
    """
    pattern = r'!\[[^\]]*\]\(([^\s\)]+)(?:\s+["\'][^"\']*["\'])?\)'
    matches = re.findall(pattern, text)
    return matches  # 闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁惧墽鎳撻—鍐偓锝庝簼閹癸綁鏌ｉ鐐搭棞闁靛棙甯掗～婵嬫晲閸涱剙顥氬┑掳鍊楁慨鐑藉磻閻愮儤鍋嬮柣妯荤湽閳ь兛绶氬鏉戭潩鏉堚敩銏ゆ⒒娴ｈ鍋犻柛搴㈡そ瀹曟粓鏁冮崒姘€梺鍛婂姦閸犳鎮￠妷鈺傜厸闁搞儺鐓堝▓鏂棵瑰鍫㈢暫婵﹤鎼晥闁搞儜鈧崑鎾澄旈崨顓狅紱闂佽宕橀崺鏍х暦閸欏绡€闂傚牊绋掑婵堢磼閳锯偓閸嬫捇姊绘担渚劸闁哄牜鍓涢崚鎺戠暆閸曗斁鍋撻崒姣椽顢旈崨顏呭闂備浇濮ら敋妞わ富鍨跺鎶芥偄閸忚偐鍘遍梺缁樏壕顓熸櫠閻㈢鍋撳▓鍨灈妞ゎ厼鍢查锝夊箻椤旇棄浜滈梺鎯х箺椤曟牠宕惔銊︹拻濞达絿顭堥ˉ蹇涙煟閹惧磭澧︾€规洑鍗冲浠嬪Ω瑜忚ぐ楣冩⒑閸涘﹥澶勯柛瀣у亾闂佽　鍋撳ù鐘差儐閻撶喖鏌熼柇锕€澧紒鐙欏洦鐓冪紓浣股戠粈鈧梻鍥ь槹缁绘繃绻濋崒姘间紑闂佹椿鍘界敮鐐哄焵椤掑喚娼愭繛鍙夛耿閺佸啴濮€閵堝懏妲梺閫炲苯澧柕鍥у楠炴帡宕卞鎯ь棜濠碉紕鍋戦崐銈嗙濠婂牆鐤悗娑櫭肩换鍡涙煕椤愶絾绀€妤犵偑鍨烘穱濠囶敍濠婂啫濡哄┑鐐茬墱閸嬪﹤顫忕紒妯诲濞撴凹鍨抽崝绋款渻閵堝棗鐏ユ繛宸幖閻ｉ攱瀵奸弶鎴濆敤濡炪倖甯婄欢鈥澄涢妸銉㈡斀闁挎稑瀚禍濂告煕婵犲啰澧电€规洘绻嗙粻娑樷槈濡偐鏋冮梻浣规偠閸庢椽宕滃▎鎴犵＜闁宠桨鎬ヨぐ鎺撳亹鐎瑰壊鍠栭崜鎵磽娴ｅ搫校濠电偛锕濠氭偄閻撳海鐣鹃梺缁橆殔閻楁粌螞閸曨厾纾奸柣鎰靛墮閸斻倗绱撳鍜冭含鐎殿喖顭烽弫鎾绘偐閼碱剙鈧偤姊虹€圭姵銆冪紒鎻掔仢閳藉濮€閿涘嫬骞堥梺璇插嚱缂嶅棝宕戦崨顖欑剨妞ゆ挾鍠嗘禍婊勩亜閹板墎鎮肩紒鐘靛仜閳规垿鏁嶉崟顐㈠箣婵犵绱曢崗妯讳繆閻戠瓔鏁婇柣锝呯灱鏍￠梻鍌氬€搁崐鎼佸磹閻戣姤鍤勯柛鎾茬劍閸忔粓鏌涢锝嗙婵☆偅锚閵嗘帒顫濋敐鍛闁诲氦顫夊ú姗€宕归崸妤冨祦婵せ鍋撴鐐叉处閹峰懘鎮烽幍顔叫掗梻鍌氬€风欢姘焽瑜旈幃褔宕卞銏＄☉铻栭柛娑卞弮閺佹粍绻濋悽闈浶㈡繛璇х畵閹繝寮撮姀鈥斥偓鐢告煥濠靛棝顎楀ù婊勭箘閳ь剝顫夊ú鏍儗閸岀偛钃熼柨娑樺濞岊亪鏌涢幘妤€瀚崹閬嶆⒒娴ｇ瓔鍤冮柛鐘虫崌瀹曞綊鎸婃径灞炬闂侀潧顭俊鍥╁姬閳ь剟姊虹粙鎸庢拱缁炬澘绉瑰顐︻敂閸啿鎷洪柣鐘叉处瑜板啴顢楅姀掳浜滈柡鍐ｅ亾闁绘濮撮悾鐑藉箮缁涘鏅梺閫炲苯澧柣锝囧厴楠炲鏁冮埀顒傜矆鐎ｎ偁浜滈柡宥囨暩缁嬪鏌￠崪鍐ɑ缂佺粯绻堝Λ鍐ㄢ槈濡嘲浜惧┑鐘冲焹閳ь剨绠撳畷濂稿Ψ閿曗偓閳ь剙鍢查埞鎴︽偐閸欏顦╅梺缁樻尪閸庣敻寮婚敓鐘茬闁靛绠戦ˇ鈺侇渻閵堝啫鍔氭い锔诲灦閸╃偤骞嬮敂缁樻櫖濠电偛妫楃换鎰矈椤斿皷鏀芥い鏃傘€嬮崝鐔虹磼椤曞懎鐏︽鐐茬箻瀹曘劎鈧稒锚閻у嫭绻濋姀锝庡殐闁告ü绮欓獮鎴﹀炊椤掆偓閽冪喖鏌曟繛鍨姶婵炲皷鏅滈妵鍕箻鐎靛摜鐒肩紓浣靛妼椤嘲顫忓ú顏勭閹兼番鍩勫鍨攽閳藉棗浜滈悗姘嵆瀹曟椽濮€閵堝懎宓嗛梺缁樻⒒缁绘繄鑺辨繝姘拺闂傚牊鐩悰婊呯磼鏉堛劍绀嬫鐐诧躬瀹曠喖顢樺☉妯瑰闂佸壊鐓堥崑鍛閺屻儲鍊垫慨姗嗗亜瀹撳棝鏌ｅ☉鍗炴珝鐎规洖鐖奸、妤佸緞鐎ｎ偅鐝濆┑鐘垫暩閸嬬偤宕归崼鏇熷仭闁靛鏅╅弫鍌滄喐閻楀牆绗氶柍閿嬪浮閺屾稓浠﹂崜褎鍣梺绋跨箰閻倿寮诲☉妯滅喖鎮╅幓鎺撶仌闂佸吋婢樺鈥愁嚕閸洖閱囨繛鎴灻‖澶娾攽閻愬弶鍣藉┑顔肩仛缁岃鲸绻濋崶顬囨煕濞戝崬鏋涙繛鍛€濆铏圭矙濞嗘儳鍓抽梺鍝ュУ閸旀瑦淇婇悽绋跨妞ゆ柨澧介弶鎼佹⒑鐟欏嫬绀冩繛澶嬬洴璺柍褜鍓熷缁樻媴閾忕懓绗￠梺瑙勭摃椤曆呭弲闂佺粯姊婚崢褔鎷戦悢鍏肩厸闁搞儮鏅涢弸鏃傜磼閳锯偓閸嬫捇姊绘繝搴′簻婵炶绠撻獮鏍煛娴艰鲸妞介、姗€鎮╅悽纰夌闯濠电偠鎻徊浠嬪箹椤愶絿澧￠梻鍌欑劍鐎笛兠鸿箛娑樺瀭闁芥ê顦介崵鏇炩攽閻樺磭顣查柛瀣椤啰鈧綆浜滈銏＄箾閸喓绠炴慨濠呮閹叉挳宕熼棃娑欐珱闂備礁鎽滄慨鐢靛枈瀹ュ鈧啫鈻庨幘绮规嫽闂佺鏈悷锔剧矈閻楀牄浜滈柡鍥╁枔婢х數鈧娲戦崡鎶界嵁濡吋瀚氶柟缁樺笧閳ь剦鍘界换娑氣偓鐢殿焾瀛濆銈嗗灥閹虫劗鍒掓繝姘嵍妞ゆ挾鍠庨弸鎴︽⒑缂佹﹩娈旈柣妤€妫涚划顓㈠箳濡や胶鍘电紒缁㈠幖閹冲酣鎮￠幇鐗堢厪闁搞儜鍐句純濡ょ姷鍋為…鍥箲閸曨垱鍎庨柟鍝勵儏閻忣亝銇?

def get_line_number(pos, line_starts):
    idx = bisect.bisect_right(line_starts, pos) - 1
    return idx + 1


class TableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.current_row = []
        elif tag in ("td", "th"):
            if self.current_row is None:
                self.current_row = []
            self.current_cell = []
            self.in_cell = True
        elif tag == "br" and self.in_cell and self.current_cell is not None:
            self.current_cell.append("\n")

    def handle_data(self, data):
        if self.in_cell and self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            text = normalize_cell_text("".join(self.current_cell or []))
            self.current_row.append(text)
            self.current_cell = None
            self.in_cell = False
        elif tag == "tr" and self.current_row is not None:
            if any(cell.strip() for cell in self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None

    def close(self):
        if self.in_cell and self.current_row is not None:
            text = normalize_cell_text("".join(self.current_cell or []))
            self.current_row.append(text)
        if self.current_row is not None and any(cell.strip() for cell in self.current_row):
            self.rows.append(self.current_row)
        super().close()


def normalize_cell_text(text):
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_html_table_rows(table_text):
    parser = TableHTMLParser()
    try:
        parser.feed(table_text)
        parser.close()
    except Exception:
        parser.rows = []

    if parser.rows:
        return parser.rows

    rows = []
    row_matches = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_text, flags=re.I | re.S)
    if not row_matches:
        row_matches = re.findall(r"<tr\b[^>]*>(.*?)(?=<tr\b|</table>|$)", table_text, flags=re.I | re.S)
    for row_html in row_matches:
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)(?:</t[dh]>|$)", row_html, flags=re.I | re.S)
        clean_cells = [normalize_cell_text(cell) for cell in cells]
        if any(clean_cells):
            rows.append(clean_cells)
    return rows


def parse_markdown_table_rows(table_text):
    rows = []
    for line in table_text.splitlines():
        stripped = line.strip()
        if not stripped or "|" not in stripped:
            continue
        cells = [normalize_cell_text(cell) for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
            continue
        if any(cells):
            rows.append(cells)
    return rows


def split_inline_attribute_value(text):
    text = normalize_cell_text(text)
    if not text:
        return "", ""
    match = re.match(r"^(.+?)\s*[:：=]\s*(.+)$", text)
    if match and len(match.group(1).strip()) >= 2:
        return match.group(1).strip(), match.group(2).strip()
    return text, ""


def looks_like_group_title(cells):
    non_empty = [cell for cell in cells if cell.strip()]
    if len(non_empty) != 1:
        return False
    text = non_empty[0]
    attr, value = split_inline_attribute_value(text)
    return not value and len(text) <= 30


def infer_unit(value):
    value = value or ""
    match = re.search(r"(rpm|RPM|hz|HZ|Hz|mpa|MPa|kPa|Pa|C|mm|cm|m|kg|MW|KW|kW|V|A|%|ppm\(w\)|ppm\(wt\))", value)
    return match.group(1) if match else None


def looks_like_attribute_name(text):
    return bool(re.search(
        r"(level|type|structure|count|quantity|ratio|speed|range|limit|threshold|parameter|model|temperature|pressure|flow|voltage|current|power|frequency|size|length|height|width|diameter|material|method|name)$",
        text or "",
        flags=re.I,
    ))


def looks_like_position_value(text):
    return bool(re.search(r"(level|install|layout|located|position|flange|stage)", text or "", flags=re.I))


def looks_like_header_row(cells):
    non_empty = [cell for cell in cells if cell.strip()]
    if len(non_empty) < 2:
        return False
    header_keywords = (
        "name", "item", "parameter", "component", "object", "standard", "unit",
        "value", "range", "limit", "requirement", "description", "remark",
    )
    keyword_hits = sum(1 for cell in non_empty if any(keyword in cell.lower() for keyword in header_keywords))
    value_like_hits = sum(1 for cell in non_empty if split_inline_attribute_value(cell)[1])
    return keyword_hits >= 2 and value_like_hits == 0


def normalize_header_attribute(header):
    header = normalize_cell_text(header)
    if header.lower() in ("", "name", "item", "parameter", "component", "object"):
        return ""
    return header


def table_rows_to_header_records(rows):
    if not rows:
        return []
    header = [normalize_cell_text(cell) for cell in rows[0]]
    records = []
    current_subject = ""

    for row_index, row in enumerate(rows[1:], start=2):
        cells = [normalize_cell_text(cell) for cell in row]
        if not any(cells):
            continue
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))

        first = cells[0] if cells else ""
        if first:
            current_subject = first
        subject = current_subject or first
        if not subject:
            continue

        for col_index in range(1, min(len(header), len(cells))):
            attribute = normalize_header_attribute(header[col_index])
            value = cells[col_index]
            if not attribute or not value:
                continue
            records.append({
                "subject": subject,
                "attribute": attribute,
                "value": value,
                "unit": infer_unit(value),
                "group": "",
                "raw_row": row,
                "row_index": row_index,
                "confidence": "high",
            })

    return records


def table_rows_to_records(rows):
    records = []
    current_group = ""
    inherited_subject = ""

    normalized_rows = [[normalize_cell_text(cell) for cell in row] for row in rows]
    if normalized_rows and looks_like_header_row(normalized_rows[0]):
        return table_rows_to_header_records(normalized_rows)

    for index, row in enumerate(normalized_rows, start=1):
        cells = list(row)
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue

        raw_row = row
        non_empty = [cell for cell in cells if cell]

        if looks_like_group_title(cells):
            current_group = non_empty[0]
            inherited_subject = current_group
            continue

        first = cells[0] if len(cells) > 0 else ""
        second = cells[1] if len(cells) > 1 else ""

        if first:
            inherited_subject = first

        subject = current_group or inherited_subject or first
        attribute = ""
        value = ""
        confidence = "medium"

        if len(cells) == 1:
            attribute, value = split_inline_attribute_value(cells[0])
            subject = current_group or ""
            if not value:
                value = cells[0]
                attribute = "\u8bf4\u660e"
                confidence = "low"
        elif first and second:
            if current_group:
                subject = current_group
                attribute = first
                value = second
            elif looks_like_position_value(second):
                subject = first
                attribute = "\u5b89\u88c5\u4f4d\u7f6e"
                value = second
            elif looks_like_attribute_name(first):
                subject = ""
                attribute = first
                value = second
            else:
                subject = first
                attribute = "\u53d6\u503c"
                value = second
        elif first and not second:
            attribute, value = split_inline_attribute_value(first)
            subject = current_group or ""
            if not value:
                value = first
                attribute = "\u8bf4\u660e"
                confidence = "low"
        elif not first and second:
            subject = inherited_subject or current_group
            attribute = "\u8865\u5145\u8bf4\u660e"
            value = second
            confidence = "low" if not subject else "medium"

        if not value and len(cells) > 2:
            value = "; ".join(cell for cell in cells[1:] if cell)

        if subject or attribute or value:
            records.append({
                "subject": subject,
                "attribute": attribute,
                "value": value,
                "unit": infer_unit(value),
                "group": current_group,
                "raw_row": raw_row,
                "row_index": index,
                "confidence": confidence,
            })

    return records


def build_table_summary(records, limit=80):
    lines = ["\u8868\u683c\u8bb0\u5f55\uff1a"]
    for idx, record in enumerate(records[:limit], start=1):
        subject = record.get("subject") or "\u672a\u660e\u786e\u5bf9\u8c61"
        attribute = record.get("attribute") or "\u8bf4\u660e"
        value = record.get("value") or ""
        if value:
            lines.append(f"{idx}. {subject}\u7684{attribute}\u4e3a{value}\u3002")
        else:
            lines.append(f"{idx}. {subject}\u5305\u542b{attribute}\u3002")
    if len(records) > limit:
        lines.append(f"... \u53e6\u6709 {len(records) - limit} \u6761\u8868\u683c\u8bb0\u5f55\u672a\u5728\u6458\u8981\u4e2d\u5c55\u5f00\u3002")
    return "\n".join(lines)


def parse_structured_table(table_text):
    result = {
        "status": "failed",
        "structured_table": None,
        "summary_text": "",
        "error": None,
    }
    try:
        lower_text = table_text.lower()
        if "<table" in lower_text or "<tr" in lower_text or "<td" in lower_text:
            table_type = "html"
            rows = parse_html_table_rows(table_text)
        else:
            table_type = "markdown"
            rows = parse_markdown_table_rows(table_text)

        if not rows:
            result["error"] = "no_table_rows_parsed"
            return result

        records = table_rows_to_records(rows)
        if not records:
            result["status"] = "partial"
            result["structured_table"] = {
                "table_type": table_type,
                "rows": rows,
                "records": [],
            }
            result["summary_text"] = "\u8868\u683c\u539f\u59cb\u884c\uff1a\n" + "\n".join(
                f"{idx}. {' | '.join(row)}" for idx, row in enumerate(rows, start=1)
            )
            return result

        result["status"] = "success"
        result["structured_table"] = {
            "table_type": table_type,
            "rows": rows,
            "records": records,
        }
        result["summary_text"] = build_table_summary(records)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def apply_structured_table_fields(chunk_obj, table_text):
    parsed = parse_structured_table(table_text)
    chunk_obj["raw_content"] = table_text
    chunk_obj["table"] = table_text
    chunk_obj["structured_parse_status"] = parsed.get("status", "failed")

    if parsed.get("structured_table") is not None:
        chunk_obj["structured_table"] = parsed["structured_table"]
    if parsed.get("error"):
        chunk_obj["structured_parse_error"] = parsed["error"]
    if parsed.get("summary_text"):
        chunk_obj["content"] = parsed["summary_text"]
    else:
        chunk_obj["content"] = table_text
    return chunk_obj


def table_text_for_chunk(table_text):
    parsed = parse_structured_table(table_text)
    return parsed.get("summary_text") or table_text

def split_text_by_tables(text):
    """
    闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁炬儳婀遍埀顒傛嚀鐎氼參宕崇壕瀣ㄤ汗闁圭儤鍨归崐鐐差渻閵堝棗绗掓い锔垮嵆瀵煡顢旈崼鐔蜂画濠电姴锕ら崯鎵不缂佹﹩娈介柣鎰綑閻忔潙鈹戦鐟颁壕闂備線娼ч悧鍡涘箠閹扮増鍋柍褜鍓氭穱濠囨倷椤忓嫧鍋撻弽顬稒鎷呴懖婵囩洴瀹曠喖顢楁担绋垮Τ濠电姷鏁告慨鏉懨洪敃鍌氱厱闁瑰濮甸崰鎰版煟濡も偓閻楀棛绮幒鎳ㄧ懓顭ㄩ埀顒勫础閹惰棄钃熸繛鎴炵懄閸庣喖鏌曟繝蹇涙闂佹鍙冨铏光偓鍦У椤ュ銇勯敂璇茬仴闁诲繑甯″缁樻媴閸濄儰铏庡銈嗗灥濞差參宕洪埀顒併亜閹哄棗浜惧銈庡幖閸㈡煡锝炶箛娑欐優閻熸瑥瀚弸鍌炴⒑閸涘﹥澶勯柛瀣閹便劑濮€閵堝棌鎷洪梺鍛婄缚閸庨亶寮告惔銏㈢閻忓繑鐗戦崑鎾崇暦閸ャ劍顔曢梻浣圭湽閸ㄥ鈥﹂崼銏笉闁挎繂顦伴悡銉╂煛閸愩劍鑲犻柟鐑橆殔缁狀垱绻涘顔荤凹闁抽攱鍨块弻娑樷槈濮楀牆濮涢梺瀹犳椤︾敻寮婚妸鈺佸嵆婵☆垵銆€閸嬫捇寮介鐐电杽闂侀潧艌閺呮粓寮插┑瀣厱閻忕偛澧介妴鎺楁煕濮椻偓娴滆泛顫忛搹鍦＜婵☆垰娴氭禍鐐寸珶閺囥埄鏁囬柣鏂挎啞閻濈兘姊绘笟鍥у缂佸鏁婚幃锟犲礃椤忓懎鏋戝┑鐘诧工閻楀棛绮堥崼鐔稿弿婵☆垰鐏濋悡鎰版煕閵堝懓瀚伴摶鏍煥濠靛棙鍣圭憸鏉块叄閺屾盯濡烽敐鍛瀳濡炪倖娲熸禍鍫曞蓟閳╁啫绶為悗锝庝簼椤旀洟姊洪幐搴㈢闁稿﹤缍婇幃锟犲即閵忥紕鍘搁梺鎼炲劘閸庤鲸淇婃總鍛婄厽闊洦娲栧暩缂備浇椴哥敮锟犲箖閳哄懎绀冩繛鏉戭儐閻忓棝姊绘担鍛婃儓闁哄牜鍓熼幆鍕敍閻愬弶妲梺鍛婃处閸ㄦ壆绮堥崘顔界叆闁哄啠鍋撻悗绗涘洤绀嬮柛鎰╁妷閺€浠嬫煟濮楀棗鏋涢柣蹇氶哺閵囧嫰顢曢姀鈺傂﹂柣鎾卞€濋弻锝夊棘濞嗙偓鐤囧┑鐐插悑閻楁粎妲愰幘瀛樺闁兼祴鍓濋崹鍫曞礆婵犲洤绠绘い鏃囨娴狀厼鈹戦悙鍙夘棞缂佺粯鍔欓、鏃堝煛娴煎崬缍婇幃鈺咁敃閿濆棛褰嬫繝娈垮枛閿曘儱顪冮挊澶屾殾闁靛濡囩弧鈧梺鍛婂姦娴滄粌顕ｉ幎鑺モ拻濞达綀顫夐妵鐔兼煕濡亽鍋㈢€规洘鍔欏畷褰掝敋閸涱厽顓跨紓鍌氬€烽悞锕傗€﹂崶鈺佸К闁逞屽墴濮婃椽骞栭悙鎻掑闂佸憡鏌ㄧ粔鎾煝瀹ュ拋鐓ラ柛顐ゅ暱閹锋椽姊洪崨濠勨槈闁挎洩绲垮▎銏ゆ焼瀹ュ棛鍘遍梺闈涱焾閸庨亶鎳滆ぐ鎺撶厓缂備焦蓱瀹曞本顨ラ悙鍙夘棥妞わ富鍣ｉ弻锟犲焵椤掍胶顩烽悗锝庡亞閸樿棄鈹戦埥鍡楃仭妞ゆ垶鐟╁畷鐢碘偓锝庡厴閸嬫挾鎲撮崟顒€浠╅梺绋挎捣閺佽顕ｆ繝姘櫜闁告稑鍊瑰Λ鍐春閳ь剚銇勯幒鎴濃偓鐟扮暦閸欏绻嗘い鏍ㄧ箖椤忕娀鏌￠崱顓犵暤闁哄被鍔岄埞鎴﹀幢閳哄倐锔剧磽娴ｅ搫啸缂侇噮鍨抽幑銏犫槈閵忊剝娅滈柟鑲╄ˉ閳ь剙鍟跨粻鎵磽閸屾瑩妾烽柛鏂跨焸閳ワ箑鐣￠柇锔界稁濠电偛妯婃禍婵嬪磻閿熺姵鐓欐繛鍫濈仢閺嬫瑩鏌ц箛鎾诲弰闁哄矉绲鹃幆鏃堝閻樺弶鐦撻梻浣告憸婵數鍠婂鍥╁崥闁绘梻鍘ч崡鎶芥煏韫囨洖啸妞ゆ柨娲弻鐔兼偂鎼达絾鎲肩紓浣割儐閸旀鎳炴潏銊х瘈婵﹩鍘搁幏娲⒑閸︻収鐒鹃悗娑掓櫊閹繝濡烽埡鍌滃幗闂婎偄娲﹀鑽ょ不閹剧粯鐓熼柨婵嗘处閺嗩剛鈧娲栧畷顒冪亽闁荤姴娲﹁ぐ鍐╁鎼粹檧鏀介柣妯虹仛閺嗏晛鈹戦鑺ュ唉鐎规洦鍨堕、娑㈡倷閸欏偊闄勭换婵嬫濞戞艾顣甸梺绋款儐閹搁箖骞夐幘顔肩妞ゆ巻鍋撴い锔规櫊濮婅櫣绮欏▎鎯у壈闂佹寧娲忛崐婵嬪箖妤ｅ啯鍊婚柤鎭掑劚娴滄粎绱掗悙顒€顎滃瀛樻倐瀵煡濮€閵堝棌鎷洪梻渚囧亞閸嬫盯鎳熼娑欐珷閻庢稒菧娴滄粓鏌曡箛濠傚⒉閻忓浚鍙冮弻宥夋寠婢舵ɑ鈻堟繝娈垮枓閸嬫捇姊洪幎鑺ユ殰闁稿鎹囬弻鐔兼煥鐎ｎ偁浠㈠┑顔硷工椤嘲鐣烽幒鎴僵闁告鍎愰弳銈夋⒒娴ｅ憡鎯堥柟鍐茬箳閹广垽宕熼鐐茬亰闂佸搫鍟悧鍡欑矆閸緷褰掓晲閸噥浠╅梺鎸庣⊕缁诲牓骞冨Δ鍛祦闁割煈鍠栨慨搴ㄦ煟鎼淬垹鍤柛鐘虫皑閸掓帡鏁愭径濠勭潉闂佺鏈〃鍛妤ｅ啯鍋℃繛鍡楃箰椤忣亞绱掗埀顒勫礃閳衡偓缁诲棝鏌ｉ幇顓烆棆闁活厽鐟ч埀顒冾潐濞叉ê顪冩禒瀣槬闁逞屽墯閵囧嫰骞掑澶嬵€栨繛瀛樼矋缁捇寮婚悢鐓庝紶闁告洦鍘滆娣囧﹪骞嗚濡插妫佹径瀣瘈濠电姴鍊搁顐︽煟椤撶喎娴柡灞糕偓宕囨殕闁逞屽墴瀹曚即寮借閺嗭附銇勯幇鍓佺暠缂佲偓鐎ｎ偁浜滈柟鐐墯濡插爼鏌涙惔锝嗘毈鐎殿喛顕ч埥澶婎煥閸涱垱婢戦梻浣筋潐閸庢娊鎮洪妸鈺佺闂侇剙绉甸埛鎴︽⒒閸喓鈯曢柟鍏煎姍閹顫濋銏犵ギ閻庢鍣崑濠囩嵁閸ヮ剙绾ч柛顭戝枤閻涒晜淇婇悙顏勨偓鏍箰閻愵剛绠鹃柍褜鍓熼弻锝呪槈濞嗘劕纾冲┑顔硷功缁垳绮悢鐓庣劦妞ゆ巻鍋撴い顓炴穿椤︽煡鎮￠妶澶嬬厪闁割偅绻冪粈宀勬煕鐎ｎ偅灏い顐ｇ箞椤㈡﹢鎮㈤崫鍕濠碉紕鍋戦崐鏍洪埡鍐濞撴埃鍋撻柕鍡曠铻栭柛娑卞幘閿涙粌鈹戦悙鏉戠仸妞ゎ厼娲弫宥堢疀濞戞瑢鎷绘繛鎾村焹閸嬫挻绻涙担鍐叉濞咃綁姊绘担鍛婂暈闁告梹鍨垮畷婊堟焼瀹ュ棗浜滈梺绋跨箰閻ㄧ兘骞忔繝姘拺缂佸瀵у﹢浼存煟閻旀潙濮傜€规洘顨呴悾婵嬪礋椤掑倸骞嶉梻浣瑰劤濞存岸宕戦崨顓犳殾鐎光偓閳ь剟鍩€椤掑喚娼愭繛鍙夛耿閹繝鍨鹃幇浣告婵犵數濮甸懝鎯ф暜闂備線娼ч敍蹇涘礋椤撶偛歇闂傚倸鍊搁崐椋庣矆娴ｉ潻鑰块梺顒€绉甸幆鐐哄箹濞ｎ剙濡肩紒鎰殜閺屸€愁吋鎼粹€茬敖婵炴垶鎸哥粔褰掑蓟閳╁啫绶炲┑鐘插閻ㄦ垿姊虹悰鈥充壕婵炲濮撮鍡涙偂閻旈晲绻嗘い鏍ㄧ箥閸ゆ瑧绱掗幓鎺濈吋闁哄矉绻濆畷鍗炍熼崗鐓庢珮缂傚倷娴囨ご鍝ユ暜閳ュ磭鏆︽繝濠傚婵挳姊婚埀顒勫箛椤撗勭稐闂傚倸鍊峰ù鍥敋閺嶎厼绐楁俊銈呮噷閳ь剙鍟换婵嬪炊瑜庨悗顒勬⒑瑜版帒浜伴柛妯圭矙瀹曟洟鎮㈤崫銉х槇闂傚倸鐗婄粙鎺楀箹閹扮増鐓涢悗锝冨妼閳ь剚娲熼崺鐐哄箣閿旇棄鈧鈧懓澹婇崰鏍р枔閸洘鈷戦柛娑橆煬閻掑ジ鏌涢妷鎴濇噽閺嬪啯绻濈喊妯活潑闁割煈鍨抽幏鍐晝閳ь剟鈥﹂崶顒€鍐€闁靛ě鍜佸晭闁诲海鎳撴竟濠囧窗閺囩姾濮冲┑鍌氭啞閻撴洟鏌曢崼婵囶棞婵炴惌鍣ｉ弻锛勪沪閻愵剛顦伴悗瑙勬礈閸樠囧煘閹达箑鐐婄憸搴敂閻斿摜绡€闁汇垽娼ф禒婊堟煟椤撶偟澧涚紒鍌氱Ч閹瑩宕归顐ｇ稐闂備浇顫夐崕鎶芥偤閵婏箑鍨旈柟缁㈠枟閻撴洟鏌熼悙顒佺稇缂佹彃顭烽弻娑㈠箻閺夋埈妫嗙紓浣介哺閹稿骞忛崨瀛樻優闁荤喐澹嗛濂告⒒娴ｇ懓顕滅€光偓閹间礁钃熼柨婵嗩槹閺呮煡鏌涘☉鍗炲箺娴滄盯姊绘担鍝ユ瀮妞ゎ偄顦靛畷褰掑垂椤旂偓娈鹃梻渚囧墮缁夋挳鎮″☉妯忓綊鏁愰崶銊ユ畬闂侀€炲苯澧婚柛銊ゅ嵆閸╃偤骞嬮敂钘変汗濡炪倖妫侀崑鎰閸パ€鏀介柣鎰▕濡插綊鏌ｉ埡濠傜仸闁靛棔绶氬浠嬵敇閻愯尙鐛╂俊鐐€栧濠氭惞鎼粹埗娲箹娴ｅ湱鍘告繝銏ｆ硾閿曪附鏅堕幇鐗堢厸闁告侗鍠氱粻妯侯熆鐟欏嫭绀嬮柟绋匡攻缁旂喎鈹戦崱娆懶ㄩ梺杞扮劍閸旀瑥鐣烽妸鈺婃晣鐟滃酣藟閹达附鈷掑ù锝勮閻掓儳螖閻樺弶鎲搁柟骞垮灲楠炴帡寮埀顒勶綖閺囥垺鐓欓柣鎴烇供濞堟棃骞嗛悢鍏尖拺閻庡湱濮伴埀顑藉亾闂佺顑嗛幑鍥蓟閻旂厧绠甸柟鐑樺灍閹稿啴姊洪柅鐐茶嫰婢ь垱銇勯弮鈧悧鐘茬暦閺夎鏃堝川椤撶姷鏆梻浣侯焾閺堫剟宕欒ぐ鎺戝惞闁哄洨鍠嗘禍婊堟煙閸濆嫭顥滃ù婊堢畺閺屟囨嚒閵堝懍妲愬┑顔硷工椤嘲鐣锋總鍛婂亜闁诡厽宸婚崑鎾诲箳閺冨倻锛滄繝銏ｆ硾椤戝棝濡靛┑瀣厸閻忕偟顭堟晶鏌ユ煙瀹勭増鍤囬柟顔惧厴瀵泛鈻庨悙顒傜▓闂備浇顕у锕傦綖婢跺⊕楦跨疀濞戞顦梺纭呮彧鐠愮喖鍩€椤戣法顦︽い顐ｇ矒閸┾偓妞ゆ帒瀚粻鏍煏韫囧鈧洜绮堥崼銉︾厵缂備焦锚缁楁岸鏌￠崱鈺佸籍婵﹤顭峰畷鎺戭潩椤戣棄浜鹃柟闂寸绾惧綊鏌ｉ幋锝呅撻柛銈呭閺屾盯顢曢敐鍡欙紩闂侀€炲苯澧剧紒鐘虫尭閻ｉ攱绺界粙娆炬綂闂佹寧绻傞幉娑㈠焺閸愵亞鐦堥梻鍌氱墛缁嬫垿鍩€椤掆偓椤兘鍨鹃敃鍌氶唶闁靛繆鈧啿骞戦梻渚€鈧偛鑻晶瀛樻叏婵犲懏顏犻柟鍙夋尦瀹曠喖顢曢姀鐘橈附绻濈喊妯活潑闁稿瀚埀顒佺煯閸楁娊鐛箛娑樼闁挎棁妫勬禍婊堟⒑缁嬭法绠洪柛瀣姍瀹曘垽顢旈崨顖滅槇闂佹眹鍨藉褎绂掑鍫熺厽闊洦姊荤粻鐐碘偓瑙勬礃缁诲倽鐏冩繛杈剧秮椤ユ挾绮诲鑸碘拺闁革富鍘剧敮娑㈡偨椤栨粌浠﹂柡鍛版硾閳藉螣閹炬娊鍙勭€规洘鍎奸¨鍌炴椤掑澧柕鍥у婵偓闁挎稑瀚уΣ鍫濐渻閵堝骸骞戦柛鏃€鍨甸悾宄邦潨閳ь剚淇婇幖浣肝ч柛銉戝拋鍋ф繝鐢靛Х椤ｎ喚妲愰弴銏犵；闁硅揪绠戠壕鍦喐韫囨搩鍤楀┑鐘叉搐缁犳氨鎲稿鍐︹偓鎺旀嫚鐟佷礁缍婇幃鈺侇啅椤旂厧澹堥梻浣芥閸熶即宕伴弽顓炶摕闁靛鍎弨浠嬫煕閳╁喛渚涙俊顐節濮婅櫣绮欓幐搴㈠闯闂佸摜濮甸悧鐘差嚕婵犳艾惟闁靛鍨洪～宥呪攽閳藉棗鐏熼悹鈧敂鐣岊浄闁革富鍘剧壕钘夈€掑顒佹悙闁哄鍊濋弻鈩冩媴缁涘娈銈庡亜缁绘劗鍙呭銈呯箰鐎氼噣顢欓崶顒佺厵闁稿繗鍋愰弳姗€鏌涢妸銉吋闁轰礁绉撮～婊堝焵椤掆偓椤繐煤椤忓嫮顔愰梺缁樺姈瑜板啴鈥栭崱娆戠＝濞撴艾娲ら弸鐔兼煟閻斿弶娅婇柣?Markdown 闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁炬儳婀遍埀顒傛嚀鐎氼參宕崇壕瀣ㄤ汗闁圭儤鍨归崐鐐烘偡濠婂啰绠荤€殿喗濞婇弫鍐磼濞戞艾骞楅梻渚€娼х换鍫ュ春閸曨垱鍊块柛鎾楀懏锛忛梺璇″瀻瀹€鈧崥瀣⒑閸濆嫮鐒跨紓宥勭窔閻涱噣宕堕澶嬫櫓闂佸吋浜介崕鏌ユ儊鎼淬劍鈷掑ù锝堫潐閵囩喖鏌涘Ο鍦煓闁诡喚鍏橀崺锟犲川椤旈棿绨垫俊鐐€栭崝褏绮婚幋锔藉€峰┑鐘叉处閻撳繐鈹戦悙鎴斿亾闁稿鎸绘穱濠囧箵閹烘柨鈪辩紓浣介哺閹稿骞忛崨鏉戠闁瑰搫绉撮ˉ姘節閻㈤潧袨闁搞劎鍘ч埢鏂库槈濞嗗繒绐為梺鎼炲労閸撴瑧澹曟繝姘厵闁硅鍔曢崥鍦偓瑙勬尫缁舵岸寮诲☉鈶┾偓锕傚箣濠靛洨浜┑鐘愁問閸犳牗顨ラ幖浣测偓鏃堝礃椤斿槈褔鐓崶銊︾缂佹劖顨婂娲川婵犲孩鐣堕梺鍝ュ枎濞硷繝骞冩ィ鍐╁€婚柦妯侯槺椤撴椽姊虹紒姗堜緵闁稿瀚粋宥嗐偅閸愨晝鍙嗗┑鐘绘涧濡繈顢撳Δ鍛厸閻庯綆鍓欓弸娑㈡煛瀹€瀣М妤犵偞顭囬埀顒勬涧閹诧繝宕抽弶搴撴斀妞ゆ梻銆嬪銉︺亜椤撶偛妲婚柣锝囧厴楠炴帡骞嬮鐔峰厞闂備胶绮幐鍝モ偓娑掓櫊椤㈡瑥顓奸崱鏇犵畾闂佺粯鍔︽禍婊堝焵椤戭剙鎳忔刊濂告煥濠靛棭妲归柍閿嬫閺屾盯寮撮妸銉ヮ潾闂?HTML 闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁炬儳婀遍埀顒傛嚀鐎氼參宕崇壕瀣ㄤ汗闁圭儤鍨归崐鐐烘偡濠婂啰绠荤€殿喗濞婇弫鍐磼濞戞艾骞楅梻渚€娼х换鍫ュ春閸曨垱鍊块柛鎾楀懏锛忛梺璇″瀻瀹€鈧崥瀣⒑閸濆嫮鐒跨紓宥勭窔閻涱噣宕堕澶嬫櫓闂佸吋浜介崕鏌ユ儊鎼淬劍鈷掑ù锝堫潐閵囩喖鏌涘Ο鍦煓闁诡喚鍏橀崺锟犲川椤旈棿绨垫俊鐐€栭崝褏绮婚幋锔藉€峰┑鐘叉处閻撳繐鈹戦悙鎴斿亾闁稿鎸绘穱濠囧箵閹烘柨鈪辩紓浣介哺閹稿骞忛崨鏉戠闁瑰搫绉撮ˉ姘節閻㈤潧袨闁搞劎鍘ч埢鏂库槈濞嗗繒绐為梺鎼炲労閸撴瑧澹曟繝姘厵闁硅鍔曢崥鍦偓瑙勬尫缁舵岸寮诲☉鈶┾偓锕傚箣濠靛洨浜┑鐘愁問閸犳牗顨ラ幖浣测偓鏃堝礃椤斿槈褔鐓崶銊︾缂佹劖顨婂娲川婵犲孩鐣堕梺鍝ュ枎濞硷繝骞冩ィ鍐╁€婚柦妯侯槺椤撴椽姊虹紒姗堜緵闁稿瀚粋宥嗐偅閸愨晝鍙嗗┑鐘绘涧濡繈顢撳Δ鍛厸閻庯綆鍓欓弸娑㈡煛瀹€瀣М妤犵偞顭囬埀顒勬涧閹诧繝宕抽弶搴撴斀妞ゆ梻銆嬪銉︺亜椤撶偛妲婚柣锝囧厴楠炴帡骞嬮鐔峰厞闂備胶绮幐鍝モ偓娑掓櫊椤㈡瑥顓兼径瀣ф嫽婵炶揪绲介幉锟犲疮閻愮儤鐓涢悘鐐额嚙婵¤法绱掗鑺ヮ梿闁靛洦鍔欓獮鎺楀箣閿濆棙鍟洪梻鍌欐祰濞夋洟宕伴幘瀛樺弿闁哄鍩堝〒濠氭煢濡警妫︾憸鐗堝笚閺呮煡鏌涢妷銉ヮ暢闁告梻顭堥—鍐Χ鎼粹€茬爱闂佸搫鎳忛惄顖炲灳閺冨牆绀冩い鏂挎瑜旈獮鏍垝閸忓浜剧€规洖娲ら幗瀣攽閻樺灚鏆╅柛瀣洴钘濋柡澶嬶紩濞差亝鍋勯柤娴嬫櫅缁侊箓姊虹涵鍛涧缂佺姵鍨圭划鍫⑩偓锝庡亖娴滄粓鏌″鍐ㄥ闁愁垱娲熼弻娑欐償椤斿鍋楀┑顔硷攻濡炰粙骞婇敓鐘参ч柛娑卞枤娴滎亪姊绘担渚劸妞ゆ垵鎳橀弫鍐敂閸曢潧娈ㄩ梺瑙勫劶濡嫬娲垮┑鐘灱濞夋盯顢栭崒鐐茬?
    闂傚倸鍊搁崐鎼佸磹閹间礁纾归柟闂寸绾惧綊鏌熼梻瀵割槮缁惧墽鎳撻—鍐偓锝庝簼閹癸綁鏌ｉ鐐搭棞闁靛棙甯掗～婵嬫晲閸涱剙顥氬┑掳鍊楁慨鐑藉磻閻愮儤鍋嬮柣妯荤湽閳ь兛绶氬鏉戭潩鏉堚敩銏ゆ⒒娴ｈ鍋犻柛搴㈡そ瀹曟粓鏁冮崒姘€梺鍛婂姦閸犳鎮￠妷鈺傜厸闁搞儺鐓堝▓鏂棵瑰鍫㈢暫婵﹤鎼晥闁搞儜鈧崑鎾澄旈崨顓狅紱闂佽宕橀崺鏍х暦閸欏绡€闂傚牊绋掑婵堢磼閳锯偓閸嬫捇姊绘担渚劸闁哄牜鍓涢崚鎺戠暆閸曗斁鍋撻崒姣椽顢旈崨顏呭闂備浇濮ら敋妞わ富鍨跺鎶芥偄閸忚偐鍘遍梺缁樏壕顓熸櫠閻㈢鍋撳▓鍨灈妞ゎ厼鍢查锝夊箻椤旇棄浜滈梺鎯х箺椤曟牠宕惔銊︹拻濞达絿顭堥ˉ蹇涙煟閹惧磭澧︾€规洑鍗冲浠嬪Ω瑜忚ぐ楣冩⒑閸涘﹥澶勯柛瀣у亾闂佽　鍋撳ù鐘差儐閻撶喖鏌熼柇锕€澧紒鐙欏洦鐓冪紓浣股戠粈鈧梻鍥ь槹缁绘繃绻濋崒姘间紑闂佹椿鍘界敮鐐哄焵椤掑喚娼愭繛鍙夛耿閺佸啴濮€閵堝懏妲梺閫炲苯澧柕鍥у楠炴帡宕卞鎯ь棜濠碉紕鍋戦崐銈嗙濠婂牆鐤悗娑櫭肩换鍡涙煕椤愶絾绀€妤犵偑鍨烘穱濠囶敍濠婂啫濡哄┑鐐茬墱閸嬪﹤顫忕紒妯诲濞撴凹鍨抽崝绋款渻閵堝棗鐏ユ繛宸幖閻ｉ攱瀵奸弶鎴濆敤濡炪倖甯婄欢鈥澄涢妸銉㈡斀闁挎稑瀚禍濂告煕婵犲啰澧电€规洘绻嗙粻娑樷槈濡偐鏋冮梻浣规偠閸庢椽宕滃▎鎴犵＜闁宠桨鎬ヨぐ鎺撳亹鐎瑰壊鍠栭崜鎵磽娴ｅ搫校濠电偛锕濠氭偄閻撳海鐣鹃梺缁橆殔閻楁粌螞閸曨厾纾奸柣鎰靛墮閸斻倗绱撳鍜冭含鐎殿喖顭烽弫鎾绘偐閼碱剙鈧偤姊虹€圭姵銆冪紒鎻掔仢閳藉濮€閿涘嫬骞堥梺璇插嚱缂嶅棝宕戦崨顖欑剨妞ゆ挾鍠嗘禍婊勩亜閹板墎鎮肩紒鐘靛仜閳规垿鏁嶉崟顐㈠箣婵犵绱曢崗妯讳繆閻戠瓔鏁婇柣锝呯灱鏍￠梻鍌氬€搁崐鎼佸磹閻戣姤鍤勯柛鎾茬劍閸忔粓鏌涢锝嗙婵☆偅锚閵嗘帒顫濋敐鍛闁诲氦顫夊ú姗€宕归崸妤冨祦婵せ鍋撴鐐叉处閹峰懘鎮烽幍顔叫掗梻鍌氬€风欢姘焽瑜旈幃褔宕卞銏＄☉铻栭柛娑卞弮閺佹粍绻濋悽闈浶㈡繛璇х畵閹繝寮撮姀鈥斥偓鐢告煥濠靛棝顎楀ù婊勭箘閳ь剝顫夊ú鏍儗閸岀偛钃熼柨娑樺濞岊亪鏌涢幘妤€瀚崹閬嶆⒒娴ｇ瓔鍤冮柛鐘虫崌瀹曞綊鎸婃径灞炬闂侀潧顭俊鍥╁姬閳ь剟姊虹粙鎸庢拱缁炬澘绉瑰顐︻敂閸啿鎷洪柣鐘叉处瑜板啴顢楅姀掳浜滈柡鍐ｅ亾闁绘濮撮悾鐑藉箮缁涘鏅梺閫炲苯澧柣锝囧厴楠炲鏁冮埀顒傜矆鐎ｎ偁浜滈柡宥囨暩缁嬪鏌￠崪鍐ɑ缂佺粯绻堝Λ鍐ㄢ槈濡嘲浜惧┑鐘冲焹閳ь剨绠撳畷濂稿Ψ閿曗偓閳ь剙鍢查埞鎴︽偐閸欏顦╅梺缁樻尪閸庣敻寮婚敓鐘茬闁靛绠戦ˇ鈺侇渻閵堝啫鍔氭い锔诲灦閸╃偤骞嬮敂缁樻櫖濠电偛妫楃换鎰矈椤斿皷鏀芥い鏃傘€嬮崝鐔虹磼椤曞懎鐏︽鐐茬箻瀹曘劑寮堕幋婵堢崺濠电姷鏁告慨鎾磹閻熸壋鏋旀慨妞诲亾婵﹦绮幏鍛村川婵犲懐顢呮俊鐐€ら崢濂告偋閸℃氨浜辨繝鐢靛█濞佳兾涘☉銏犳辈闁挎洖鍊归悡鐔兼煛閸愩劌鈧摜鏁崜浣虹＜闁绘瑥鎳愮粔顕€鏌″畝瀣М妤犵偛娲幃褔宕奸悢鍏兼殬濠碉紕鍋戦崐銈夊磹閵堝宸濇い鏍ㄧ玻缁卞啿鈹戦悙鑸靛涧缂傚秮鍋撳┑鐐叉嫅缁插潡寮灏栨闁靛骏绱曢崢浠嬫⒑閸愬弶鎯堥柨鏇樺€濋幃姗€鏁冮崒娑氬幗濠电偞鍨靛畷顒勫几閵堝鐓冪憸婊堝礈閵娧呯闁糕剝绋戠粣妤佷繆閵堝懏鍣圭痪鎯х秺閺岋綁骞嬮敐鍛呮捇鏌涙繝鍌滀粵缂佺粯鐩獮瀣倻閸℃洜妫俊鐐€曠换鎰版偋閸℃瑧鐭嗛柛顐ゅ枂娴滄粓鏌￠崒婵囩《婵絿鍋ら弻锟犲焵椤掍胶顩烽悗锝庡亞閸樹粙姊鸿ぐ鎺戜喊闁搞劋鍗抽幆鍐洪鍛幍闂佷紮绲介懟顖氭毄缂傚倷娴囨ご鍝ユ暜閿熺姰鈧礁鈻庨幘鏉戞異闂佸疇顕栭崗娆撳磹濠靛钃熸繛鎴欏灩缁犳娊鏌熼幑鎰彧閻犲洨鍋涜灃闁绘﹢娼ф禒锕傛煥濮樿埖鐓熼柨婵嗘搐閸樺鈧娲栭悥鍏间繆閹间焦鏅滈悹鍥у级濞呮姊婚崒姘偓鎼佸磹妞嬪孩顐芥慨姗嗗墻閻掔晫鎲歌箛娑樼闁靛繈鍊曢柋鍥煏婢跺牆鍔ら柨娑欑懇濮婃椽宕崟顒€绐涙繝娈垮櫍閺€杈╃矙婢跺鍚嬮柛娑变簼閺傗偓闂備胶绮敋缁剧虎鍙冮妴鍌炲蓟閵夛妇鍘介梺瑙勫礃濞夋盯鍩㈤崼銉︾厸閻忕偛澧介埥澶愭煃閽樺妲告い顐ｇ矒瀹曞崬鈻庤箛鎾剁ɑ闂傚倸鍊风粈渚€骞楀鍫濈獥閹艰揪绲惧畷鏌ユ煙鐎电校鐎规洖寮剁换娑㈠箣濞嗗繒浠鹃梺鎼炲€曢崯鎾蓟瀹ュ棙濮滈柟娈垮枛婵′粙姊虹拠鑼闁绘妫濋崺鐐哄箣閿旂粯鏅╃紒缁㈠幘閸忔娆㈤弶鎴旀斀闁绘劕寮剁€氬懐绱掗幓鎺斾虎閸楅亶鏌熼悧鍫熺凡缂佺姵濞婇弻鐔煎箹椤撶偛绠洪悗鐐瑰€栧钘夘潖閾忕懓瀵查柡鍥╁仜閳峰姊洪懡銈呮瀭闁稿孩鐓￠獮鍫ュΩ閳哄倸娈愰梺鍐叉惈閸熶即鏁嶅┑瀣拺缂佸瀵у﹢浼存煟閻旀繂鎳忕€氳霉閻撳海鎽犻柣鎾存礋閺岋絽螣閾忕櫢绱炴繝鈷€灞藉⒋闁哄矉绻濆畷銊╊敇閻樿尙鍘介梻浣筋嚃閸犳牗鏅堕懞銉ь浄闁挎洖鍊圭€电姴顭跨捄鍝勵殭闁?(segment_text, is_table)
    """
    lines = text.splitlines()
    segments = []
    i = 0
    n = len(lines)

    def is_md_table_start(idx):
        if idx >= n - 1:
            return False
        line = lines[idx].strip()
        next_line = lines[idx + 1].strip()
        if '|' not in line or re.fullmatch(r'[\s|:-]+', line):
            return False
        if re.fullmatch(r'[\s|:-]+', next_line) and '|' in next_line:
            cells = [c.strip() for c in next_line.strip('|').split('|')]
            if any(re.fullmatch(r':?-{3,}:?', c) for c in cells if c):
                return True
        return False

    def is_html_table_start(idx):
        return '<table' in lines[idx].lower()

    def collect_table(start_idx, table_type):
        end_idx = start_idx
        if table_type == 'md':
            while end_idx < n:
                line = lines[end_idx].strip()
                if not line:
                    break
                if '|' not in line and not re.fullmatch(r'[\s|:-]+', line):
                    break
                end_idx += 1
        else:  # html
            depth = 1
            end_idx = start_idx + 1
            while end_idx < n and depth > 0:
                lower_line = lines[end_idx].lower()
                if '<table' in lower_line:
                    depth += 1
                if '</table>' in lower_line:
                    depth -= 1
                if depth > 0 and lower_line.lstrip().startswith('#'):
                    break
                end_idx += 1
        table_text = '\n'.join(lines[start_idx:end_idx])
        return end_idx, table_text

    while i < n:
        line = lines[i].strip()
        if is_md_table_start(i):
            end, table_text = collect_table(i, 'md')
            segments.append((table_text_for_chunk(table_text), False))
            i = end
        elif is_html_table_start(i):
            end, table_text = collect_table(i, 'html')
            segments.append((table_text_for_chunk(table_text), False))
            i = end
        else:
            start = i
            while i < n and not is_md_table_start(i) and not is_html_table_start(i):
                i += 1
            para_text = '\n'.join(lines[start:i])
            if para_text.strip():
                segments.append((para_text, False))
    return segments

def normalize_tables_for_chunking(text):
    segments = split_text_by_tables(text)
    return "\n\n".join(seg_text.strip() for seg_text, _ in segments if seg_text.strip())


def split_content_by_size(text, chunk_size):
    text = (text or "").strip()
    if not text:
        return []
    size = max(1, int(chunk_size or 800))
    return [text[start:start + size] for start in range(0, len(text), size)]


def make_chunk_obj(
    *,
    chunk_id,
    doc_name,
    content,
    chapter,
    section,
    subsection,
    section_path,
    source_line,
    source_file,
    file_id,
    file_version_id,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    chunk_obj = {
        "id": str(chunk_id),
        "chunk_name": doc_name,
        "content": content,
        "chapter": chapter,
        "section": section,
        "subsection": subsection,
        "section_path": section_path,
        "source": source_line,
        "file": source_file,
        "chunk_id": str(chunk_id),
        "file_id": file_id,
        "file_version_id": file_version_id,
        "is_active": True,
        "chunk_uid": f"{file_version_id}::{chunk_id}",
    }
    chunk_obj.update(
        build_chunk_common_fields(
            source_type=source_type,
            file_format=file_format,
            chunk_type=chunk_type,
            source_record_type=source_record_type,
            source_record_id=source_record_id,
        )
    )
    img_paths = extract_image_paths(content)
    if img_paths:
        chunk_obj["image_paths"] = img_paths
    return chunk_obj


def parse_markdown_hierarchy(
    content,
    chunk_size,
    doc_name,
    source_file,
    file_id,
    file_version_id,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    lines_with_breaks = content.splitlines(keepends=True)
    line_starts = []
    pos = 0
    for line in lines_with_breaks:
        line_starts.append(pos)
        pos += len(line)
    if not line_starts:
        line_starts = [0]

    titles = []
    in_code_block = False
    for idx, line in enumerate(lines_with_breaks):
        stripped = line.rstrip("\n").lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block and stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            if level >= 1:
                start_pos = line_starts[idx]
                end_pos = start_pos + len(line)
                titles.append((start_pos, end_pos, level, stripped.strip()))

    all_chunks = []
    chunk_id = 0

    def append_text_chunks(block_text, chapter, section, subsection, section_path, source_line):
        nonlocal chunk_id
        normalized_text = normalize_tables_for_chunking(block_text)
        for chunk_text in split_content_by_size(normalized_text, chunk_size):
            all_chunks.append(
                make_chunk_obj(
                    chunk_id=chunk_id,
                    doc_name=doc_name,
                    content=chunk_text,
                    chapter=chapter,
                    section=section,
                    subsection=subsection,
                    section_path=section_path,
                    source_line=source_line,
                    source_file=source_file,
                    file_id=file_id,
                    file_version_id=file_version_id,
                    source_type=source_type,
                    file_format=file_format,
                    chunk_type=chunk_type,
                    source_record_type=source_record_type,
                    source_record_id=source_record_id,
                )
            )
            chunk_id += 1

    if not titles:
        append_text_chunks(content, "", "", "", "0.0.0", get_line_number(0, line_starts))
        return all_chunks

    titles.append((len(content), len(content), 0, ""))
    heading_counters = [0, 0, 0, 0, 0, 0, 0]
    cur_chapter_name = ""
    cur_section_name = ""
    cur_subsection_name = ""
    cur_chapter_num = 0
    cur_section_num = 0
    cur_subsection_num = 0

    for i in range(len(titles) - 1):
        title_start, title_end, level, title_text = titles[i]
        next_title_start = titles[i + 1][0]

        if 2 <= level <= 6:
            heading_counters[level] += 1
            for j in range(level + 1, 7):
                heading_counters[j] = 0

        clean_title = title_text.lstrip("#").strip()
        if level == 1:
            cur_chapter_name = clean_title
        elif level == 2:
            cur_chapter_name = clean_title
            cur_chapter_num = heading_counters[2]
            cur_section_name = ""
            cur_section_num = 0
            cur_subsection_name = ""
            cur_subsection_num = 0
        elif level == 3:
            cur_section_name = clean_title
            cur_section_num = heading_counters[3]
            cur_subsection_name = ""
            cur_subsection_num = 0
        elif level == 4:
            cur_subsection_name = clean_title
            cur_subsection_num = heading_counters[4]
        elif level > 4:
            cur_subsection_name = clean_title

        full_block = content[title_end:next_title_start].lstrip("\n")
        if not full_block.strip():
            continue

        append_text_chunks(
            full_block,
            cur_chapter_name,
            cur_section_name,
            cur_subsection_name,
            f"{cur_chapter_num}.{cur_section_num}.{cur_subsection_num}",
            get_line_number(title_start, line_starts),
        )

    return all_chunks
def process_single_document_flow(
    input_file_path,
    chunk_size,
    file_id,
    file_version_id,
    source_type,
    file_format,
    chunk_type,
    source_record_type=None,
    source_record_id=None,
):
    doc_name, content = load_single_file(input_file_path)
    if not content:
        print("Empty content, skipped.")
        return []
    all_chunks = parse_markdown_hierarchy(
        content,
        chunk_size,
        doc_name,
        os.path.basename(input_file_path),
        file_id,
        file_version_id,
        source_type,
        file_format,
        chunk_type,
        source_record_type,
        source_record_id,
    )
    return all_chunks

def main():
    parser = argparse.ArgumentParser(description="Markdown document chunking tool")
    parser.add_argument("--input", "-i", required=True, help="Input Markdown file path (.md)")
    parser.add_argument("--output", "-o", required=True, help="Output JSON file path")
    parser.add_argument("--chunk_size", "-s", type=int, default=800, help="Chunk size in characters")
    parser.add_argument("--file_id", required=True, help="File ID")
    parser.add_argument("--file_version_id", required=True, help="File version ID")
    parser.add_argument(
        "--source_type",
        default="manual_document",
        choices=["manual_document", "standard_document", "work_order", "maintenance_record", "time_series_event"],
        help="Business source type for the chunk",
    )
    parser.add_argument(
        "--file_format",
        required=True,
        help="Original input file format, such as pdf / md / xlsx / csv / docx / parquet",
    )
    parser.add_argument(
        "--chunk_type",
        default="document_section",
        choices=["document_section", "table_row_summary", "case_summary", "sensor_event_summary"],
        help="Chunk content type",
    )
    parser.add_argument("--source_record_type", default=None, help="Original record type")
    parser.add_argument("--source_record_id", default=None, help="Original record ID")
    args = parser.parse_args()

    start_time = time.time()

    if os.path.isdir(args.input):
        print(f"Error: input path is a directory, please provide a Markdown file: {args.input}")
        return

    chunks = process_single_document_flow(
        args.input,
        args.chunk_size,
        args.file_id,
        args.file_version_id,
        args.source_type,
        args.file_format,
        args.chunk_type,
        args.source_record_type,
        args.source_record_id,
    )
    if chunks:
        save_json(chunks, args.output)
        print(f"Document chunking completed: {args.output}")
        print(f"Generated {len(chunks)} chunks")
    else:
        print("No chunks generated")

    elapsed = time.time() - start_time
    print(f"\n=== Document chunking elapsed: {elapsed:.2f}s ===")

if __name__ == "__main__":
    main()
