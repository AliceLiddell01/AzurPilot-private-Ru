from __future__ import annotations

import ast
import base64
import io
import json
import re
import string
import subprocess
import tokenize
import warnings
import zlib
from pathlib import Path


BASE_SHA = "f0b9a3c475eac6dff6c5073cdd2f5771f7c72f07"
FINAL_V2_SHA256 = "9e3b3dd9abd1ed2c20c2777aa8f5b5f574197af49a975914495554db8aae6850"
PRODUCTION_FILES = (
    "module/commission/commission.py",
    "module/commission/project.py",
    "module/dorm/buy_furniture.py",
    "module/dorm/dorm.py",
    "module/event/campaign_abcd.py",
    "module/freebies/battle_pass.py",
    "module/freebies/data_key.py",
    "module/freebies/freebies.py",
    "module/freebies/mail_white.py",
    "module/freebies/supply_pack.py",
    "module/island/island.py",
    "module/island/island_air_drop.py",
    "module/island/island_business.py",
    "module/island/island_cargo_preparation.py",
    "module/island/island_daily_gather.py",
    "module/island/island_daily_interact.py",
    "module/island/island_daily_order.py",
    "module/island/island_farm.py",
    "module/island/island_fishery.py",
    "module/island/island_grill.py",
    "module/island/island_juu_coffee.py",
    "module/island/island_juu_eatery.py",
    "module/island/island_manufacture.py",
    "module/island/island_mine_forest.py",
    "module/island/island_pearl_sell.py",
    "module/island/island_rancher.py",
    "module/island/island_restaurant.py",
    "module/island/island_season.py",
    "module/island/island_select_character.py",
    "module/island/island_shop_base.py",
    "module/island/island_teahouse.py",
    "module/island/ui.py",
    "module/meowfficer/buy.py",
    "module/research/preset_generator.py",
    "module/research/project.py",
    "module/research/research.py",
    "module/research/rqueue.py",
    "module/research/selector.py",
    "module/research/ui.py",
    "module/reward/reward.py",
    "module/shop/base.py",
    "module/shop/clerk.py",
    "module/shop/shop_core.py",
    "module/shop/shop_general.py",
    "module/shop/shop_guild.py",
    "module/shop/shop_medal.py",
    "module/shop/shop_merit.py",
    "module/shop/shop_reward.py",
    "module/shop/shop_voucher.py",
    "module/shop/ui.py",
    "module/shop_event/clerk.py",
    "module/shop_event/item.py",
    "module/shop_event/shop_event.py",
    "module/shop_event/ui.py",
    "module/storage/box_disassemble.py",
    "module/storage/storage.py",
    "module/storage/ui.py",
    "module/tactical/tactical_class.py",
)

# Exact base line ranges for the 1296 Final-v2 candidates owned by the six
# consolidated Phase-2B owner groups. Generated from authoritative Final v2;
# compressed only to keep this disposable verifier small.
_RANGES_B85 = """c-n<q+j8SLlKq!?R%{V>V1K2fL!q+eD$V#dvfMot6Z`Lb@+1i)X{!5yHi8!r%*)A>nb3d#@$&Zg`F#HN@b>a@`S`fJy_(_wy#M;g|Ni!ybLD{J_rq__y8{jxC4a!d5w0d)t+-lwwZ#Jtj&QZfRX_Oy4vq>&Jm+xE@f_aaot|qLH5vJEz#*fM4me~K@kHbk30D)ZVg;Tn2v}d>`hqVQ@m$5Ia@26O@oK9WRg9vW4mddS7<rBYMly56e3A1-kE@<neZ(kolrhR2Rg5@a!V`&4<aWRzqk?x7zN6yV%4Z>$NV&v@`<mRBAfZGFC9FPi^$9a4&YTiPiK843I5@(&%ySjbR(Up0(*cK!Aiqrc<=_uEWCZzT$}gvQz`;?)NM^}cbLN_JDF+-fs+gs6mYOh19N{WunxW}TO&6@baP`H-0}hT7Mv0?>QQ@d!ByTH_ZlQDwq+2N6;_-y%69HEPuZE0K=BQyL>nqTCq0UR54me~~aJ9(Q3Jq3jutF}Ca;Y_AB(pR~s8K=<D{Wk9!>Srr)j~Mn;HY3E?`Y8-aQy!JVaH$IuW$c6Kip<#i4a$$xYE?1PAV2cPZWB}2?|S87*toQney@N`tt4j=da__=k@h+`@EhP|7#O`Xrea-jA-+i+;j3>AA)6iTBgU>cz%uXoQQK#&*8qv`y%FxZMz)#Gp|C-PvqmEQ%9XT$jnh@78f$mAQKI+>N>4GAA0s)M|fZ0Rmc{Hj0#4DBYdalcLvyhp#2AkE>Ls<UKG@efEAHg5n=1mhqYG%AtDhX!dfD&C1&_%mUkrRK2i6^v*4A9UYRFY{Y0w=T4kbD4zTq=kAWR#wL{#8!^rLY$NBa4?cwzDe!Bd4J)XWlJT9hzg=P91+|4}kbUmNHU(O%jzMpQl=kxLX^zku20_{<G@Blgh-m9;LhY?-@6gc)KdpzAv$A6uF%`*YD6ttE_2uctT4uq1N;rV;>EQ4i5kbR^QpaZJlK;^{*3GY4sa=JVp|NMEmE#?fvuQVE%g`-)x2_`kENx^6c$1Y$*194cgQzM0$d77Dr33!@-uP~%aM}YD?l@~B`;LHI^4m8#P=Lk-N1cd6<xFA-VN`N`y*jxX{=llEfFDT{VUyCT~K`j#Dgr5#G3$%p;QNHCvBhCO))MM}gjy~b>QC>d}pUjBnpi>;?)t8Uw)9d5>Z^3|wh#IMKShb^7JLn9Dj0#3Yy$-XfIEJu>zo!Y2a#}}ESc0c1co?RqhZ#>SI9DjZ3BsBvEFi#Fp+e;J>;ME0L~!G~A}Y!%M4XE}mvCR=eTZU#Q7pi&L)rNFgqS($2wqH2G%*;S5$PHcvqa7kf%%b`pMX$_2$f)`iH4f+ZHeEOprAwzWvn@K%{e1JX2wU{mw8_<z=9GsQ4#lPyv0g$)d-0ALrg9(u|g9wu`45D<+6%X1-?+|3<Y{C)LVsSDm7DqT9v3(v8u{dRY<7P^+#lDF!x4tZ_s(8&Kpd-(X<<o9S7YE1W5kqILeq7<q$6A569wnv*Xd|W~*~a&L=$&(a0Z-0rG6sXRCyUI>!bv`kL?A_3?DM9v`o7?+Ys6tNGFR=3<-(Ym{};c@S=BP7bi>h>H&5bri3QSYKS@g~O*i{d5?Iqj9(b)G0(An6{&_IVi+YA^ZaU026eT!ruFk@1Gx+*K>#LF@^Hnx{lUhU7oDV`W<{R(-&cBzJ!(EdFa_w&mJ>-GME4c6Ubmff$tPrI3iJE#7Yhp?lv(F7MW;~2?|csZ-Pn_HJV@;si{xkiKa0Q5rLvLoDtre>AjW#!aEqNGjx`zv@(IaP2_GsjY8BY;3<VX#d-p+61NKGE1a(a7b<a~;%ep9D%PRq0iUh(*@mFe7&IDYZcgnVe2%!#u=*CV;chYlAanH%KGx`C4Sb-H4>YW*aa9fG-DuqnT4>Zl1Dk1NGj5v1izf9V#;`evK>_kd$6~c=wv&_lo!;*y(h+IgB%U^@r%htfQ~2sdO~x&0sE-osHSAZ3`;?axV{mE>_$OgSL#f0$r-2U6BIoCL1G*!B)P218kJquBS0@p(a%xvj5-1nucZn|q23a_p_}Qe6Cf^e0a4qN%6&<4ZSX3vAGEdZbq99RJBuY^d$jI}F^ila^f@<WCj;oBT=9@_pPR^NxWG2;15?LjcRYFoFf9U$6qpUj0!k02LE0=?egN_B=vZ7m-gf?qNn-LP_56ALd8WEfX0;iF{NrL1wLvoS?In9Ec7`#)9$3H<BGF5<26$DZ+64WXRTSa72R3>$*LY{e#0KHSB$3F@0j1#U2nP|!;n&=vT?t;|A>H6dC_<lXVpRT9d$n8xAP=(eItpI(V&=+9pftGG!K1@k1DF_=S9Su-hpw>ddxx{lowUEOqzDF>j=#-m-xhKtz5zA@S5&j<O?-6Q^RWoLsBj$<8sKq3#C2=hYY&4OMCWNXK;~Ld~wTW0OW+GN{Pdbm_sK1UsPPd=u>q6GSHJE?{H#Ha^=#mS2ZNMBt4p5_P*ldqj;`!?MdnFVv{v6klZA7sw7P(IP9Kg@Dx-Ov%ng<`8Q!0>iPj+sSJ}ALc36|U;7*X^d!u3s4F9aDnk>%5%d3;*{KM&;R0h}Q?WznE&{EonMC2VLOh#?C_0bdmOMS?mCIw4D9L-j&_k@6E<QCNx2EhQOzTU4@*m>wC^M=BTLrIAi57^4sqp`b_&nZP(Qau{NwAto^Y<lEnv#^v>PzMguLFUf1O$<LetzzygHORlV$-l3D5CWQx@h>$OvzzMeE9BtXq7VrT_K0unK7*hors7UyMryrQizvue_79428gK3+37Q7_VOCr1^(o2j_?s#1UGl*mc38;{W3JEMTk!2>t+r)TliXAY56t}f2JgfOPLQE>_+Es$7C*=}&N9H>+LRw}-%P_f2lgs!H9Dk#<x9ekfXIMXtU#NGp8UbNYek(LKY+SWxYeNqs(~=Ow2xICSBZD&Twm6y1cz~;vf<?(*-(VJ`Nj6-DI2f?tz!e9`C}?d24yv)(IFG4Q!YRl!V!*keZUpyW#EZc0NbHVc#A?M-8(=IjP6qdlbl(W~jdb6brG}u^fpfIzVI8BgnLtbud5NiPLe7bDPLOV*bPYE(_S9Kw1lq^6L3Yf?RS98GzVOvLR(TzwR%X=7m@h{iHH=v8Wc4F&vOe5LmXTpJnO0+}oe1a}&zD^9o-IC|dI@cT0}u6kMJ~8-bZM;`m_(#W#0;V4xJI}x`~-fHh>i&#m-unk<+`v&!VC70NFGd43mBUjqcSit6B9FppDFykl2-=WWx|~>B_&E(%1QZtfPK1rAha&ED2;qds-kc$!kVxgEe*nT{5Dd7rj$+U14V|Ix)W)M0ERoYwfO^Lqc0mr5X24}N5EAAS4>jOT1(rK6F5wD8*s#YEv!u)GES6nLexx*noB}<PfkCsm*?jtBPQdxcjrGpKaUS@PfzDwB^Ju4j1gYs=tVZBK)g}=HA0iqx^p9RzbLT)f=wDzfLqKRP1P1L#WSW16Sh^OMM)mG<+b+Pxul2hc>0b};oz5}x-^rGzxwd=>1M2HK)0}9O?b@bN`^3P%4I$C7tXD`2Q0x)_ucsA^!oV(1~AI<cvQ){nFB)M=j;k^6y;&LKCVSJno|)fh?80_;FS;=;o&&$ukP}CK0du&&mRj#aY?c#r%8$AB<34eY934y#^OWV6eO^ER!clG9?yhlR9sCZRm#C(&G(E44u3L*_#RHWXC=_0G{tpr7})j&^zTXI{d~GUA3x6Bl@?ysi`_F2YtdT}{-E^;cOcwIX^qvkcwlAkOGOt#a}=8Ch77hqPN%tQSArzknrC-k`&wdJ_i(!57-B!VIl~i29SI7|sz9iSeY{aUGQKt{w3s<?W~0CXtH^x<lOHJ=XvH9aRtM5*%RWYhcEGP+`Y{5qQJE$boi?>v!xo_ENwYf1xxz;IJFy>TnrNnmH=3R&Mj>osKY|^(PL5&<Y700=9c8R9tDV4QGF>J^Te;MAofm<!Fi=YRuGxFRvxUzV$f{6QrZWlcm$`lm;#suN*)Y1FcwA4f4<j9g&(Y;ZS(F8%bwWAHqN59!r8p5*FkrKarAIfiim#Bkg0s<`haP<L8w<p_tZ;#I3Oc5exioD8W)7S=fW!pnH@$@3mO*Y3Qe0OUdde8^9f99rd;Gn#Ke`2&Il8jWe+&WW9R$OKVbL+JVX8NFO~>A3J-xp$DiQEet!o)eHNE@Z3eEygdEL(k@FoIpLN<_?4U8&nhR0=kTt?i?lQ#5Vsf_;_+RD_{;th9g4DsUh2}@g|1|+KGXR1E-_Hx)ng|6IQH;~Gs79Im!HPBT9Tr1GEOclU@N>{E)hel-pDkUff#D}0U5@~7B0(gfZ13qR0M=vvtbSSu`f&i$C+PfU7uZc#8RW#QK*N>%c%28tj2d=hySF|7J)5q9BGkf9vo1f1Qx8uXllN1@c(|dfSSFqiYf&KEHMU0p`=MHyY`Sb1l`2F;;u$d+FEfz3J6qYsgh6b_*@096)!BEy7+V<48PXGxP5={HhNw?Ik+k(g>LJXVq7Iwt?lSyg~f9N4(FbM!|Om$tr7`z>J6lq6Nm)5ehb>+d}@eaP%Caa^7g3QUawWFJmgme6I>)vpFzSrhoz0d)2pK99VWW%X@c7}kO8BmQ=V=b9$u^|pm1l^<n7zlL}*v3|$9^y>yhO0`8U}d@FP1x&=PP5fVI^ENzy@PYiaZICU1WJ=GN2JS~1u$O-^HLGlf_OoOR+yocAcn{mDstKqHK5`g5y-ZX@ZO@^e=swFvS9UvtFKsT<w`50TPfX&HCL{=Lcf*ztq2j7A)<m3RZ^k~Nmfd-Mm!O9>kvpkNz`uB3HLR<Z*Dc+^S;~p^z-erJ6{$(8Xl@urqUadd0qS+j)n(3pD;Ch*(;)(MPL+xMq$|9sM&+NggC7$g{ExVVcJxvM&t$j7Qd-g4RFRe&Gbw(9$<x$i+7`OFN5q>qx$YT1Kp$VzgM;Fv3fW&LIIAi%K5zXl(Mt>d3VM-TaMDSG0(r8-~N1hx;&h(vVp6EzVwUiYY=XcnLPab_AaB_@yGe~d_CRXde4F7<lX!27Bkt%mN)#Y!|})!niQ$qD$tsJiiX!L!7ah+lDdB{1r=m9IQAB`43<^RjeApv810XpWOVFQ*{!6Gv~*-TuudQAx{D4#^8_^0JI7My1a(vu*qVsz{AO4?!qmL)1Oz*@(R@%gI4v1WFa4NQb<`*EZHeEOa9^tHl3PMBPuh?eJ$8Jrb|y{ra<}9LV4w^#h*VB~4c7Ax%{?9c`}6#{po~N*XZ9)CEa5M=M=($2Icc$KhjrH3&O4RNbfaDxXSVUxdmdvilc})x{J3w+u$_*vJ9t`l2g}|z0<aIVtFWzeh*{q|EA9S1ns_6x@3uQ60^4vcn>X4y|2$nE=YPvqIlyW$TjiE5cg}HJ4JCoOz5ja+fOb0F?goqYQ(o_mHiaeikg+bIdEX4p8+C}?YQZS?m~C~VH8?7Gh$5sV0Z0*?ZlOaw@fuGM9pbj-*Q3brHF!y=oBh}b*pUG{0^Q=IWpEHdta<ZQ56|c8vL#Py0-7ux6ryEsf^JP1?VPb!2=L(uA5Qip)Ur0QUfQ&o%xK+=PttI>Sr&=)!`pRBte35gd$(&K=!IY5ET%_g_6jlv)lKF;I)mLxq(QlAYMOU_UY;K}>7Fg2Za0*UCtuEweT}BFEL$Ozt8{c2`wVTHxZB6<oBQv$UTz!7X}sIJd+lzbA_DS3xh{~>o1&h!{`RK%<L%RwTDA&X_2)<PmClgN-chlLIQJpc$n&Pr+Vd2XSt9EoJbrDZZB{zS$*}FQgJ7s*2KDvR$9Y$>b(y(Gy~OQaVOlmev_or~?F@Y%>)4zNF6Dw&pJXeMUoWQDlKv`km<3*L=cQRz<R<~#8Fo-AKeL`*c8Ay<#ctkATf8&(7%r@Pt_D}h0@hu569)!Xr=@8nZ=SziwnOQTCfVpT?{kuEPHWa<dM>ql@B%%EtzY7+skn{(@nawFUd7GedCv+=(SB^0&hsupi}6dL!EX8(dw=?-kCD3X?d>%=gM#F)9=aj_^+xcqXL{_A=vHUs$&FryS(<l(gq(hyzkPrE&++l{ar*c;zkGi_Z?@L=jXuksjTtsNCm0qmY<;wnOx%|3Tib@v_@$HbmanUu|KG96CO<~m*yiZ-xrIH9mG#hOe-{E);5OTVqWm5p-s|ZN8`;}bZ&-EDHOIa$Q<dG>y#SjF%FXv0Uc2R;QPfc$um;PeY-2kKotw-4?|F{_$gkhjXUG1qzKg(AWy$81R0NE|`nOecvF58jyLm57r!A0;O+bUAzyWll;6U`z!8Cx<&3>lK<Mp@G!|n2LdhUbc!}ItZ$zo26%NfJ$Kc2Htve*9`=@mw*OajBErZ=Ktl%I;kb%DXuG51VjDI8cD*sFk}j6ZinB{c&{BqT=h<5)Hkch87+c2}66;0@DRHmaDq9Q2f_CzGU&MpD5@>rV&^g<-+|U=3LpS5=^lLTwbdS)rR17;mBRR)pKia9cr>DrtQU!UWkg!k1Tmc}2jf3^-;>^!y{X-~aFb0qcZOZv"""

# Reviewed human-facing companion changes may be added here only after the
# verifier reports them. Tuple: (path, base_start_line, base_end_line).
COMPANION_ALLOWLIST: set[tuple[str, int, int]] = set()

_STRING_TOKEN_HEAD = re.compile(r"(?i)^([rubf]*)(\'\'\'|\"\"\"|\'|\")")
_PERCENT_PATTERN = re.compile(r"%(?:\([^)]+\))?[#0\- +]*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlL]?[diouxXeEfFgGcrsa%]")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _decode_ranges() -> dict[str, list[list[int]]]:
    raw = zlib.decompress(base64.b85decode(_RANGES_B85.encode("ascii")))
    ranges = json.loads(raw.decode("utf-8"))
    assert sum(len(entries) for entries in ranges.values()) == 1296
    assert set(ranges) == set(PRODUCTION_FILES)
    return ranges


CANDIDATE_RANGES = _decode_ranges()


def _allowed(path: str, start: int, end: int) -> bool:
    if (path, start, end) in COMPANION_ALLOWLIST:
        return True
    return any(start <= candidate_end and end >= candidate_start for candidate_start, candidate_end in CANDIDATE_RANGES[path])


def _source_at(ref: str, path: str) -> str:
    return _git("show", f"{ref}:{path}")


def _dump_optional(node: ast.AST | None) -> str | None:
    return None if node is None else ast.dump(node, include_attributes=False)


def _docstrings(tree: ast.AST) -> list[tuple[str, str, str | None]]:
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [
        (type(node).__name__, getattr(node, "name", "<module>"), ast.get_docstring(node, clean=False))
        for node in ast.walk(tree)
        if isinstance(node, holders)
    ]


def _comments(source: str) -> list[str]:
    return [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]


def _imports(tree: ast.AST) -> list[str]:
    return [
        ast.dump(node, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _definitions(tree: ast.AST) -> list[tuple[str, str, str]]:
    result = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append((type(node).__name__, node.name, ast.dump(node.args, include_attributes=False)))
        elif isinstance(node, ast.ClassDef):
            result.append(("ClassDef", node.name, ast.dump(node.bases, include_attributes=False)))
    return result


def _call_shapes(tree: ast.AST) -> list[tuple[str, int, tuple[str | None, ...]]]:
    return [
        (ast.dump(node.func, include_attributes=False), len(node.args), tuple(keyword.arg for keyword in node.keywords))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]


def _numeric_literals(tree: ast.AST) -> list[object]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float, complex))
        and not isinstance(node.value, bool)
    ]


def _percent_placeholder_signatures(tree: ast.AST) -> list[tuple[str, ...]]:
    result = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            result.append(tuple(_PERCENT_PATTERN.findall(node.left.value)))
    return result


def _dot_format_placeholder_signatures(tree: ast.AST) -> list[tuple[tuple[str | None, str, str | None], ...]]:
    formatter = string.Formatter()
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "format":
            continue
        value = node.func.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        fields = tuple(
            (field_name, format_spec, conversion)
            for _literal, field_name, format_spec, conversion in formatter.parse(value.value)
            if field_name is not None
        )
        result.append(fields)
    return result


def _fvalue_signature(node: ast.FormattedValue) -> tuple[str, int, str | None]:
    return ast.dump(node.value, include_attributes=False), node.conversion, _dump_optional(node.format_spec)


def _fstring_static_text(node: ast.JoinedStr) -> str:
    return "".join(value.value for value in node.values if isinstance(value, ast.Constant) and isinstance(value.value, str))


def _assert_ast_allowlisted(
    left: ast.AST,
    right: ast.AST,
    file_path: str,
    ast_path: str,
    unauthorized: list[str],
) -> None:
    if type(left) is not type(right):
        raise AssertionError(f"AST node type changed at {file_path}:{ast_path}: {type(left).__name__} != {type(right).__name__}")

    if isinstance(left, ast.Constant):
        if isinstance(left.value, str) and isinstance(right.value, str):
            if left.kind != right.kind:
                raise AssertionError(f"String literal kind changed at {file_path}:{ast_path}")
            if left.value != right.value:
                start = getattr(left, "lineno", 0)
                end = getattr(left, "end_lineno", start)
                if not _allowed(file_path, start, end):
                    unauthorized.append(f"{file_path}:{start}-{end} ordinary-string base={left.value!r} head={right.value!r}")
            return
        if left.value != right.value or left.kind != right.kind:
            raise AssertionError(f"Non-string constant changed at {file_path}:{ast_path}: {left.value!r} != {right.value!r}")
        return

    if isinstance(left, ast.JoinedStr):
        left_values = [value for value in left.values if isinstance(value, ast.FormattedValue)]
        right_values = [value for value in right.values if isinstance(value, ast.FormattedValue)]
        if len(left_values) != len(right_values):
            raise AssertionError(f"f-string formatted-value count changed at {file_path}:{ast_path}")
        for index, (l_value, r_value) in enumerate(zip(left_values, right_values, strict=True)):
            if _fvalue_signature(l_value) != _fvalue_signature(r_value):
                raise AssertionError(f"f-string expression/conversion/format-spec changed at {file_path}:{ast_path}[{index}]")
        for value in [*left.values, *right.values]:
            if not (
                isinstance(value, ast.FormattedValue)
                or (isinstance(value, ast.Constant) and isinstance(value.value, str))
            ):
                raise AssertionError(f"Unexpected f-string AST segment at {file_path}:{ast_path}: {type(value).__name__}")
        if _fstring_static_text(left) != _fstring_static_text(right):
            start = getattr(left, "lineno", 0)
            end = getattr(left, "end_lineno", start)
            if not _allowed(file_path, start, end):
                unauthorized.append(
                    f"{file_path}:{start}-{end} f-string-static base={_fstring_static_text(left)!r} "
                    f"head={_fstring_static_text(right)!r}"
                )
        return

    for field in left._fields:
        l_value = getattr(left, field)
        r_value = getattr(right, field)
        child_path = f"{ast_path}.{field}"
        if isinstance(l_value, ast.AST):
            if not isinstance(r_value, ast.AST):
                raise AssertionError(f"AST/scalar mismatch at {file_path}:{child_path}")
            _assert_ast_allowlisted(l_value, r_value, file_path, child_path, unauthorized)
        elif isinstance(l_value, list):
            if not isinstance(r_value, list) or len(l_value) != len(r_value):
                raise AssertionError(f"AST list shape changed at {file_path}:{child_path}")
            for index, (l_item, r_item) in enumerate(zip(l_value, r_value, strict=True)):
                item_path = f"{child_path}[{index}]"
                if isinstance(l_item, ast.AST):
                    if not isinstance(r_item, ast.AST):
                        raise AssertionError(f"AST/scalar mismatch at {file_path}:{item_path}")
                    _assert_ast_allowlisted(l_item, r_item, file_path, item_path, unauthorized)
                elif l_item != r_item:
                    raise AssertionError(f"AST scalar-list value changed at {file_path}:{item_path}: {l_item!r} != {r_item!r}")
        elif l_value != r_value:
            raise AssertionError(f"AST scalar changed at {file_path}:{child_path}: {l_value!r} != {r_value!r}")


def _string_token_shape(value: str) -> tuple[str, str]:
    match = _STRING_TOKEN_HEAD.match(value)
    if not match:
        raise AssertionError(f"Cannot parse string token prefix/quote: {value[:20]!r}")
    return match.group(1).lower(), match.group(2)


def _token_signature(source: str) -> tuple[list[tuple[int, str]], list[tuple[str, str]]]:
    structural = []
    ordinary_string_shapes = []
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.STRING:
            ordinary_string_shapes.append(_string_token_shape(token.string))
            structural.append((token.type, "<STRING>"))
            continue
        if fstring_middle is not None and token.type == fstring_middle:
            continue
        structural.append((token.type, token.string))
    return structural, ordinary_string_shapes


def test_phase2b_allowlisted_structural_parity() -> None:
    _git("fetch", "--no-tags", "--filter=blob:none", "--depth=1", "origin", BASE_SHA)

    changed_production = tuple(
        line for line in _git("diff", "--name-only", BASE_SHA, "HEAD", "--", "module").splitlines() if line
    )
    assert set(changed_production) == set(PRODUCTION_FILES), (
        f"Production changed-file set mismatch: missing={sorted(set(PRODUCTION_FILES) - set(changed_production))}; "
        f"extra={sorted(set(changed_production) - set(PRODUCTION_FILES))}"
    )
    assert len(changed_production) == 58

    numstat = _git("diff", "--numstat", BASE_SHA, "HEAD", "--", "module").splitlines()
    assert len(numstat) == 58
    for row in numstat:
        added, deleted, path = row.split("\t", 2)
        assert added != "-" and deleted != "-", f"Binary production diff is forbidden: {path}"
        assert int(added) == int(deleted), f"Non-symmetric production diff in {path}: +{added}/-{deleted}"

    unauthorized: list[str] = []
    verified = 0
    for path in PRODUCTION_FILES:
        base_source = _source_at(BASE_SHA, path)
        head_source = Path(path).read_text(encoding="utf-8")
        assert "\ufffd" not in base_source and "\ufffd" not in head_source, f"U+FFFD detected: {path}"
        assert len(base_source) - len(base_source.rstrip("\n")) == len(head_source) - len(head_source.rstrip("\n")), (
            f"EOF newline parity changed: {path}"
        )

        base_tree = ast.parse(base_source, filename=f"{BASE_SHA}:{path}")
        head_tree = ast.parse(head_source, filename=f"HEAD:{path}")
        assert _docstrings(base_tree) == _docstrings(head_tree), f"Docstring changed: {path}"
        assert _comments(base_source) == _comments(head_source), f"Comment changed: {path}"
        assert _imports(base_tree) == _imports(head_tree), f"Import structure changed: {path}"
        assert _definitions(base_tree) == _definitions(head_tree), f"Symbol/signature structure changed: {path}"
        assert _call_shapes(base_tree) == _call_shapes(head_tree), f"Call target/shape changed: {path}"
        assert _numeric_literals(base_tree) == _numeric_literals(head_tree), f"Numeric literal changed: {path}"
        assert _percent_placeholder_signatures(base_tree) == _percent_placeholder_signatures(head_tree), (
            f"Percent-format placeholder signature changed: {path}"
        )
        assert _dot_format_placeholder_signatures(base_tree) == _dot_format_placeholder_signatures(head_tree), (
            f".format() placeholder signature changed: {path}"
        )
        assert _token_signature(base_source) == _token_signature(head_source), f"Non-string token structure changed: {path}"
        _assert_ast_allowlisted(base_tree, head_tree, path, "root", unauthorized)
        verified += 1

    if unauthorized:
        raise AssertionError("Unauthorized string changes outside Final-v2 candidate ranges:\n" + "\n".join(unauthorized))

    assert verified == 58
    warnings.warn(
        "PHASE2B_STRUCTURAL_PROOF_PASS: 58/58 production files; every changed string site is allowlisted by the "
        "1296-candidate Final-v2 owner ranges or reviewed companion allowlist; control-flow/non-string AST fields, "
        "f-string FormattedValue expressions/conversions/format-specs, imports, symbols/signatures, calls, numeric "
        "literals, .format/% placeholders, comments/docstrings, non-string tokens and EOF parity are preserved; "
        f"Final-v2 SHA-256={FINAL_V2_SHA256}",
        stacklevel=1,
    )
