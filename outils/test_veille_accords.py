# -*- coding: utf-8 -*-
"""Régressions de veille A3C V20 (incluant les acquis V17), sans accès Internet.

Les extraits de chiffres de 2024, 2025, 2026 correspondent aux textes de
Légifrance ; les enveloppes XML de tests sont synthétiques.
"""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import veille_accords as v


GRILLE_2025 = '''
Hauts-de-France Accord du 25 novembre 2024 relatif aux indemnités de petits déplacements.
En application du code du travail : ouvriers du bâtiment IDCC 1596 et 1597.
L'indemnité de repas est fixée à 14 €.
Indemnité de transport et indemnité de trajet :
Au 1er janvier 2025
Zone 1 2,68 € 1,55 €
Zone 2 6,38 € 3,11 €
Zone 3 9,86 € 4,66 €
Zone 4 12,96 € 6,22 €
Zone 5 16,57 € 7,78 €
'''
GRILLE_2026 = GRILLE_2025.replace('25 novembre 2024','1er décembre 2025').replace('2025\nZone', '2026\nZone')
for a, b in {'2,68': '2,71','1,55': '1,57','6,38': '6,44','3,11':'3,14',
             '9,86':'9,96','4,66':'4,71','12,96':'13,09','6,22':'6,28',
             '16,57':'16,74','7,78':'7,86'}.items():
    GRILLE_2026 = GRILLE_2026.replace(a, b)
GRILLE_JUILLET = GRILLE_2026.replace('1er décembre 2025','10 juillet 2026').replace('1er janvier 2026', '1er septembre 2026')
for a,b in {'2,71':'2,76', '6,44':'6,56', '9,96':'10,14', '13,09':'13,33', '16,74':'17,04'}.items():
    GRILLE_JUILLET = GRILLE_JUILLET.replace(a,b)
JORF = '''Arrêté du 22 octobre 2026 portant extension d'accords régionaux (Hauts-de-France)
conclus dans le cadre des conventions collectives nationales des ouvriers du bâtiment
(nos 1596 et 1597). Vu l'accord régional du 10 juillet 2026 relatif aux
indemnités de petits déplacements. Les stipulations sont rendues obligatoires.
JO du 31 octobre 2026. TRST2627911A JORFTEXT000055037883.'''


def xml(titre, texte, ident):
    return f'<TEXTE><ID>{ident}</ID><TITRE_TXT>{titre}</TITRE_TXT><CONTENU>{texte}</CONTENU></TEXTE>'.encode('utf-8')


def archive_temporaire(elements):
    dossier = tempfile.TemporaryDirectory()
    chemin = Path(dossier.name) / 'test.tar.gz'
    with tarfile.open(chemin, 'w:gz') as tar:
        for nom, blob in elements.items():
            donnees = blob if isinstance(blob, bytes) else blob.encode('utf-8')
            entree = tarfile.TarInfo(nom)
            entree.size = len(donnees)
            tar.addfile(entree, io.BytesIO(donnees))
    return dossier, str(chemin)


class TestVeilleV17(unittest.TestCase):
    def test_index_kali(self):
        liens = v.archives_disponibles('<a href="KALI_20261109-212854.tar.gz">x</a>'
            '<a href="Freemium_kali_global_20261108-140000.tar.gz">y</a>', 'KALI')
        self.assertTrue(liens['global'].endswith('.tar.gz'))
        self.assertEqual(len(liens['quotidiens']), 1)

    def test_index_jorf_sans_global(self):
        liens = v.archives_disponibles('<a href="JORF_20261108-210452.tar.gz">x</a>'
            '<a href="Freemium_jorf_global_20261108-140000.tar.gz">y</a>', 'JORF')
        self.assertEqual(len(liens['quotidiens']), 1)
        self.assertIsNone(liens['global'])

    def test_filtre_accord(self):
        self.assertTrue(v.est_accord_cible(GRILLE_2026))
        self.assertFalse(v.est_accord_cible('Accord Hauts-de-France ouvriers des travaux publics IDCC 2614 petits déplacements'))
        self.assertFalse(v.est_accord_cible('Bretagne ouvriers du bâtiment IDCC 1596 petits déplacements'))

    def test_filtre_jorf(self):
        self.assertTrue(v.est_extension_cible(JORF))
        self.assertFalse(v.est_extension_cible('Arrêté portant extension Bourgogne-Franche-Comté ouvriers du bâtiment IDCC 1596'))

    def test_date_de_signature(self):
        self.assertEqual(v.date_accord(GRILLE_JUILLET), '2026-07-10')
        self.assertEqual(v.date_accord(GRILLE_2026), '2025-12-01')
        self.assertEqual(v.date_accord(GRILLE_2025), '2024-11-25')

    def test_2025_reconstitution(self):
        x = v.extraire_grille(GRILLE_2025)
        self.assertEqual(x['etat'], 'proposition_a_valider')
        self.assertEqual(x['date_effet_proposee'], '2025-01-01')
        self.assertEqual(x['repas'], '14.00')
        self.assertEqual(x['transport'], v.TEMOINS['2024-11-25']['transport'])
        self.assertEqual(x['trajet'], v.TEMOINS['2024-11-25']['trajet'])

    def test_2026_reconstitution(self):
        x = v.extraire_grille(GRILLE_2026)
        self.assertEqual(x['etat'], 'proposition_a_valider')
        self.assertEqual(x['date_effet_proposee'], '2026-01-01')
        self.assertEqual(x['transport'], v.TEMOINS['2025-12-01']['transport'])
        self.assertEqual(x['trajet'], v.TEMOINS['2025-12-01']['trajet'])

    def test_juillet_2026_reconstitution(self):
        x = v.extraire_grille(GRILLE_JUILLET)
        self.assertEqual(x['etat'], 'proposition_a_valider')
        self.assertEqual(x['date_effet_proposee'], '2026-09-01')
        self.assertEqual(x['transport'], v.TEMOINS['2026-07-10']['transport'])

    def test_ancien_texte_en_listes(self):
        texte = '''Bâtiment Hauts-de-France IDCC 1597 indemnités de petits déplacements.
        Indemnité de repas : l'indemnité de repas est fixée à 11 €.
        Indemnité de transport : zone 1 : 2,17 € ; zone 2 : 5,16 € ; zone 3 : 7,98 € ; zone 4 : 10,48 € ; zone 5 : 13,40 €.
        Indemnité de trajet : zone 1 : 1,41 € ; zone 2 : 2,81 € ; zone 3 : 4,23 € ; zone 4 : 5,64 € ; zone 5 : 7,05 €.'''
        x = v.extraire_grille(texte)
        self.assertEqual(x['etat'],'proposition_a_valider')
        self.assertEqual(x['trajet'][2],'4.23')

    def test_incomplet_non_valide(self):
        self.assertEqual(v.extraire_grille('Repas : 14 € et zone 1 : 2,76 €')['etat'], 'non_reconstitue')

    def test_ambiguite_multi_regions_non_validee(self):
        deux = GRILLE_JUILLET + '\nPour les départements de l’Aisne, de l’Oise et de la Somme, autre grille :\n' + GRILLE_JUILLET
        self.assertEqual(v.extraire_grille(deux)['etat'], 'non_reconstitue')

    def test_archive_regroupe_deux_idcc(self):
        extrait = GRILLE_2026.replace('IDCC 1596 et 1597', 'IDCC 1596')
        x1 = xml('Hauts-de-France Accord du 1er décembre 2025 relatif aux indemnités de petits déplacements', extrait, 'KALIARTI000053973298')
        x2 = xml('Hauts-de-France Accord du 1er décembre 2025 relatif aux indemnités de petits déplacements', extrait.replace('1596','1597'), 'KALIARTI000053973270')
        temp, f = archive_temporaire({'a/KALIARTI000053973298.xml': x1, 'b/KALIARTI000053973270.xml': x2})
        try:
            resultat = v.analyser_archive(f, 'KALI', 'archive fictive')
            self.assertEqual(resultat['fichiers_xml_examines'], 2)
            self.assertEqual(len(resultat['groupes']), 1)
            self.assertEqual(resultat['groupes'][0]['extraction']['etat'],'proposition_a_valider')
            self.assertEqual(v.bilan_temoins(resultat['groupes'])['2025-12-01']['etat'],'conforme')
        finally:
            temp.cleanup()

    def test_extension_jorf_et_reference(self):
        blob = xml('Arrêté du 22 octobre 2026 portant extension (Hauts-de-France)', JORF, 'JORFTEXT000055037883')
        temp, f = archive_temporaire({'JORF_2026/JORFTEXT000055037883.xml': blob})
        try:
            resultat = v.analyser_archive(f, 'JORF', 'archive fictive')
            self.assertEqual(len(resultat['groupes']), 1)
            self.assertIn('JORFTEXT000055037883', resultat['groupes'][0]['textes_legifrance'][0])
            self.assertTrue(resultat['groupes'][0]['objet_extension_a_verifier'])
        finally:
            temp.cleanup()

    def test_pas_de_montants_en_apprentissage_auto(self):
        x = {'cle':'accord-2026-07-10','famille':'KALI', 'date_signature':'2026-07-10'}
        self.assertEqual(v.candidats_nouveaux([x], 'complet'), [])
        x['date_signature'] = '2026-11-12'
        self.assertEqual(len(v.candidats_nouveaux([x], 'complet')), 1)

    def test_ticket_limite_a_une_alerte_unique(self):
        x = {'cle':'accord-2026-11-12','titre':'Accord à vérifier', 'famille':'KALI',
             'date_signature':'2026-11-12','source_archive':'test', 'textes_legifrance':[],
             'extrait':'', 'extraction':{'etat':'non_reconstitue','repas':None,'transport':None,'trajet':None}}
        with patch.object(v,'tickets_deja_signales', return_value={'accord-2026-11-12'}), \
             patch.object(v,'appel_github') as requete:
            nouveaux, doublons = v.creer_tickets([x], 'depot', 'jeton')
            self.assertEqual((nouveaux,doublons),(0,1))
            requete.assert_not_called()


class TestVeilleV19(unittest.TestCase):
    """Contrôles indépendants des sources, sur archives synthétiques hors ligne."""

    def test_index_bocc_classe_par_date_de_publication(self):
        html = '''<a href="CCO20260036.complet.taz">36</a> 2026-11-09 14:40 2.4M
        <a href="CCO20260035.complet.taz">35</a> 2026-11-04 16:40 3.5M
        <a href="CCO20260032.complet.taz">32</a> 2026-11-03 12:40 3.5M
        <a href="CCO20260034.complet.taz">34</a> 2026-11-03 16:40 2.5M'''
        obtenu = v.archives_bocc_disponibles(html, limite=2)
        self.assertEqual(len(obtenu['recents']), 2)
        self.assertTrue(obtenu['recents'][-1].endswith('36.complet.taz'))
        self.assertFalse(obtenu['temoin_2026_present'])

    def test_temoins_bocc_absents_signales_sans_supposition(self):
        x = v.archives_bocc_disponibles('<a href="CCO20260038.complet.taz">x</a>', 1)
        self.assertTrue(x['temoin_2026_present'])
        self.assertEqual(x['nombre_total'], 1)

    def test_detection_extension_sans_identifiant_preinscrit(self):
        t = JORF.replace('JORFTEXT000055037883', '')
        self.assertEqual(v.accords_etendus_dans_texte(t), [
            {'date_signature': '2026-07-10', 'objet': 'indemnites_de_petits_deplacements'}])

    def test_refus_de_conclure_a_partir_de_simple_date(self):
        t = 'Arrêté extension Hauts-de-France ouvriers bâtiment IDCC 1596 du 10 juillet 2026 relatif aux salaires.'
        self.assertEqual(v.accords_etendus_dans_texte(t), [])

    def test_jour_de_publication_lu_dans_metadonnees(self):
        contenu = b'<JORF><META><DATE_PUBLI>2026-10-31</DATE_PUBLI></META></JORF>'
        self.assertEqual(v.date_publication_xml(contenu), '2026-10-31')
        self.assertIsNone(v.date_publication_xml(b'<JORF><DATE>2026-10-31</DATE></JORF>'))

    def test_temoignage_reel_non_pretendu_sans_archive_jorf(self):
        statut = v.bilan_jorf_temoins([], [])['extension_accord_10_juillet_2026']
        self.assertEqual(statut['etat'], 'non_evaluable_sans_archive_historique')
        self.assertFalse(statut['preuve_archive_xml'])

    def test_jorf_extension_cible_parmi_salaires(self):
        self.assertEqual(v.qualification_texte_jorf(JORF), 'extension_deplacements_a_controler')
        salaires = '''Arrêté portant extension d'accords régionaux Hauts-de-France
        des ouvriers du bâtiment IDCC 1596 et 1597 concernant les salaires.'''
        self.assertEqual(v.qualification_texte_jorf(salaires), 'extension_regionale_generale_a_examiner')
        self.assertEqual(v.qualification_texte_jorf('Arrêté extension Rhône-Alpes ouvriers bâtiment 1596'), 'hors_champ')

    def test_bocc_archive_tar_gz_conserve_document_a_controler(self):
        texte = 'Accord régional Hauts-de-France du 10 juillet 2026 relatif aux indemnités de petits déplacements des ouvriers du bâtiment IDCC 1596 et 1597'
        archive, chemin = archive_temporaire({'bocc/accord.xml': xml('Accord du 10 juillet 2026', texte, 'BOCCTEST0001')})
        try:
            a = v.analyser_bocc(chemin, 'BOCC fictif')
            self.assertEqual(len(a['groupes']), 1)
            g = a['groupes'][0]
            self.assertEqual(g['cle'], 'accord-2026-07-10')
            self.assertEqual(g['extraction']['etat'], 'source_bocc_a_verifier')
            self.assertIsNone(g['extraction']['transport'])
        finally:
            archive.cleanup()

    def test_bocc_zip_pris_en_charge(self):
        import zipfile
        texte = GRILLE_JUILLET
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'fichier.taz'
            with zipfile.ZipFile(f, 'w') as z:
                z.writestr('bulletin/accord.xml', xml('Hauts-de-France Accord du 10 juillet 2026 relatif aux indemnités de petits déplacements', texte, 'BOCC'))
            a = v.analyser_bocc(str(f), 'BOCC ZIP fictif')
            self.assertEqual(len(a['groupes']), 1)
            self.assertEqual(a['groupes'][0]['famille'], 'BOCC')

    def test_pdf_pages_isolees_pour_eviter_faux_positifs(self):
        with patch.object(v, 'extraire_pdf', return_value=('Bretagne travaux publics.\fHauts-de-France petits déplacements ouvriers bâtiment 1596.\f', None)):
            parties, anomalie = v.segments_bocc('bulletin.pdf', b'%PDF')
            self.assertIsNone(anomalie)
            self.assertEqual(len(parties), 2)
            self.assertFalse(v.est_accord_cible(parties[0][1]))
            self.assertTrue(v.est_accord_cible(parties[1][1]))

    def test_donnees_bocc_et_kali_separees(self):
        commun = {'cle': 'accord-2026-07-10', 'date_signature':'2026-07-10',
                  'textes_legifrance': [], 'fichiers_sources': [], 'extraction': {'etat':'non_reconstitue'}}
        a = {'groupes': [dict(commun, famille='KALI')]}
        b = {'groupes': [dict(commun, famille='BOCC')]}
        self.assertEqual(len(v.fusionner_groupes([a, b])), 2)

    def test_execution_retroactive_trois_archives_sans_modifier_baremes(self):
        # Ce scénario contrôle réellement la commande qui sera lancée sur GitHub.
        import json
        element_kali = xml('Hauts-de-France Accord du 1er décembre 2025 relatif aux indemnités de petits déplacements', GRILLE_2026, 'KALIARTI000053973298')
        element_jorf = xml('Arrêté du 22 octobre 2026 portant extension d’accords régionaux Hauts-de-France', JORF, 'JORFTEXT000055037883')
        element_bocc = xml('Hauts-de-France Accord du 10 juillet 2026 relatif aux indemnités de petits déplacements', GRILLE_JUILLET, 'BOCC')
        a1, p1 = archive_temporaire({'k.xml': element_kali})
        a2, p2 = archive_temporaire({'j.xml': element_jorf})
        a3, p3 = archive_temporaire({'b.xml': element_bocc})
        try:
            with tempfile.TemporaryDirectory() as dossier:
                referentiel = Path(dossier) / 'baremes.json'
                referentiel.write_text('{"records": []}', encoding='utf-8')
                rapport = Path(dossier) / 'rapport_veille.json'
                avant = referentiel.read_bytes()
                resultat = v.execution(['--mode', 'retroactif', '--rapport', str(rapport),
                        '--archive-kali-locale', p1, '--archive-jorf-locale', p2,
                        '--archive-bocc-locale', p3])
                self.assertEqual(resultat, 0)
                donnees = json.loads(rapport.read_text(encoding='utf-8'))
                self.assertEqual(donnees['version'], '20.0')
                self.assertEqual(len(donnees['archives']), 3)
                self.assertFalse(donnees['baremes_modifies'])
                self.assertEqual(avant, referentiel.read_bytes())
                self.assertTrue(any(d['famille'] == 'JORF' for d in donnees['documents']))
                self.assertEqual(donnees['temoins_jorf']['extension_accord_10_juillet_2026']['etat'], 'correspondance_xml_locale_a_authentifier')
        finally:
            a1.cleanup(); a2.cleanup(); a3.cleanup()


class TestRetrospectifEtPersistantV19(unittest.TestCase):
    def test_index_jorfsimple_et_exclusion_stock_massif(self):
        html = ('<a href="Freemium_jorf_simple_20261108-140000.tar.gz">stock</a>'
                '<a href="JORFSIMPLE_20261109-212854.tar.gz">flux</a>')
        trouve = v.archives_disponibles(html, 'JORFSIMPLE')
        self.assertEqual(len(trouve['quotidiens']), 1)
        self.assertIsNone(trouve['global'])
        self.assertTrue(trouve['quotidiens'][0].startswith(v.SOURCE_JORFSIMPLE))

    def test_journal_officiel_31_octobre_XML_synthetique(self):
        texte = ('''Arrêté du 22 octobre 2026 portant extension d'accords régionaux Hauts-de-France
        des ouvriers du bâtiment (n° 1596 et 1597). Article 3 : sont rendues obligatoires
        les stipulations de l'accord régional (Hauts-de-France) du 10 juillet 2026 relatif
        aux indemnités de petits déplacements.''')
        bloc = ('''<JORF><META><ID>JORFTEXT000055037883</ID><DATE_PUBLI>2026-10-31</DATE_PUBLI>
        <TITREFULL>Arrêté du 22 octobre 2026 portant extension d’accords régionaux (Hauts-de-France)</TITREFULL></META>
        <CONTENU>''' + texte + '''</CONTENU></JORF>''').encode('utf-8')
        dossier, chemin = archive_temporaire({'jorf/JORFTEXT000055037883.xml': bloc})
        try:
            res = v.analyser_archive(chemin, 'JORFSIMPLE', 'archive XML artificielle pour test')
            self.assertEqual(len(res['groupes']), 1)
            doc = res['groupes'][0]
            self.assertEqual(doc['date_publication_xml'], '2026-10-31')
            self.assertEqual(doc['accords_vises'][0]['date_signature'], '2026-07-10')
            self.assertEqual(v.bilan_jorf_temoins(res['groupes'], [res])['extension_accord_10_juillet_2026']['etat'], 'correspondance_xml_locale_a_authentifier')
            self.assertEqual(doc['famille'], 'JORFSIMPLE')
        finally:
            dossier.cleanup()

    def test_plusieurs_accords_ne_pas_faussement_associer(self):
        texte = ('''Arrêté d'extension Hauts-de-France ouvriers bâtiment 1596 1597.
        Vu l'accord régional (Hauts-de-France) du 10 juillet 2026 relatif aux salaires.
        Vu l'accord régional (Hauts-de-France) du 12 décembre 2026 relatif aux indemnités de petits déplacements.
        Arrête : les accords sont rendus obligatoires.''')
        extraits = v.accords_etendus_dans_texte(texte)
        self.assertEqual([r['date_signature'] for r in extraits], ['2026-12-12'])

    def test_archive_journal_reconnue_sans_reference_legifrance(self):
        t = JORF.replace('JORFTEXT000055037883', '')
        doc = v.donnees_xml(xml('Arrêté du 22 octobre 2026 portant extension Hauts-de-France', t, 'X'))
        self.assertTrue(v.est_extension_cible(*doc))

    def test_etat_ajout_et_non_duplication(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            fichier = Path(d) / 'suivi' / 'etat.json'
            etat = v.charger_etat(str(fichier))
            v.enregistrer_archive(etat, 'JORFSIMPLE', v.SOURCE_JORFSIMPLE + 'JORFSIMPLE_20261110-002231.tar.gz')
            v.enregistrer_archive(etat, 'JORFSIMPLE', v.SOURCE_JORFSIMPLE + 'JORFSIMPLE_20261110-002231.tar.gz')
            self.assertEqual(len(etat['archives_traitees']['JORFSIMPLE']), 1)
            self.assertEqual(etat['derniere_date_jorf'], '2026-11-10')
            fichier.parent.mkdir(parents=True, exist_ok=True)
            fichier.write_text(json.dumps(etat), encoding='utf-8')
            self.assertEqual(v.charger_etat(str(fichier))['derniere_date_jorf'], '2026-11-10')

    def test_rupture_de_couverture_signalee(self):
        ancien = {'derniere_date_jorf': '2026-11-04'}
        liens = [v.SOURCE_JORFSIMPLE + 'JORFSIMPLE_20261109-212854.tar.gz']
        self.assertIn('RUPTURE DE COUVERTURE', v.ecarts_de_couverture(ancien, liens)[0])
        self.assertEqual(v.ecarts_de_couverture({'derniere_date_jorf': '2026-11-08'}, liens), [])

    def test_ticket_lacune_explicitement_non_accord(self):
        titre = v.description_ticket({'famille': 'CONTROLE', 'extrait': 'Rupture de couverture'})
        self.assertIn('ne signale pas un nouvel accord', titre)
        self.assertIn('Rupture de couverture', titre)

    def test_jamais_de_date_extraite_d_un_fichier_de_telechargement(self):
        self.assertIsNone(v.date_publication_xml(b'<JORF><NOM>JORFSIMPLE_20261031-002231.tar.gz</NOM></JORF>'))

    def test_ne_publie_jamais_les_baremes(self):
        with tempfile.TemporaryDirectory() as d:
            baremes = Path(d) / 'baremes.json'
            baremes.write_text('{"records": []}', encoding='utf-8')
            original = baremes.read_bytes()
            source, chemin = archive_temporaire({'j.xml': xml('Arrêté portant extension Hauts-de-France', JORF, 'JORFTEXT000055037883')})
            try:
                with patch.object(v, 'lire_index', return_value='<html></html>'):
                    rapport = Path(d) / 'rapport.json'
                    code = v.execution(['--mode', 'retroactif', '--archive-jorfsimple-locale', chemin,
                                        '--archive-kali-locale', chemin, '--archive-bocc-locale', chemin,
                                        '--rapport', str(rapport)])
                # KALI et BOCC n'ont pas de grilles pertinentes ; la preuve JORF lue suffit.
                self.assertEqual(code, 0)
                self.assertEqual(baremes.read_bytes(), original)
            finally:
                source.cleanup()


class TestControleHistoriqueV20(unittest.TestCase):
    """Scénarios de régression V20 sur fichiers SYNTHÉTIQUES et flux simulés.

    Leur réussite ne prouve jamais le contenu de l'archive officielle réelle.
    """

    def setUp(self):
        self.titre = 'Arrêté du 22 octobre 2026 portant extension d’accords régionaux Hauts-de-France (bâtiment 1596 et 1597)'
        self.corps = '''Arrêté portant extension d'accords régionaux (Hauts-de-France)
        des ouvriers du bâtiment (IDCC 1596, 1597).
        Vu l'accord régional (Hauts-de-France) du 10 juillet 2026 relatif aux salaires ;
        Vu l'accord régional (Hauts-de-France) du 10 juillet 2026 relatif aux indemnités de petits déplacements ;
        Article 1 Sont rendues obligatoires les stipulations de l'accord régional
        (Hauts-de-France) du 10 juillet 2026 relatif aux salaires.
        Article 3 Sont rendues obligatoires, pour tous les employeurs et salariés
        compris dans le champ d'application professionnel des conventions collectives
        nationales des ouvriers du bâtiment, les stipulations de l'accord régional
        (Hauts-de-France) du 10 juillet 2026 relatif aux indemnités de petits déplacements.
        Article 5 L'extension prend effet à compter de la publication du présent arrêté.
        TRST2627911A.'''

    def piece(self, corps=None, ident='JORFTEXT000055037883', date_pub='2026-10-31'):
        from xml.sax.saxutils import escape
        corps = self.corps if corps is None else corps
        return (f'<TEXTE><META><ID>{ident}</ID><DATE_PUBLI>{date_pub}</DATE_PUBLI>'
                f'<NOR>TRST2627911A</NOR><TITREFULL>{escape(self.titre)}</TITREFULL></META>'
                f'<CONTENU>{escape(corps)}</CONTENU></TEXTE>').encode('utf-8')

    def archive(self, elements=None):
        contenu = elements or {'jorf/TEXT/55/JORFTEXT000055037883.xml': self.piece()}
        dossier, fichier = archive_temporaire(contenu)
        return dossier, Path(fichier).read_bytes()

    def analyse_bytes(self, donnees, authentifie=False, **kwargs):
        return v.scanner_stock_jorf_vers_temoins(
            io.BytesIO(donnees), '2026-10-31', 'archive fictive de tests',
            authentifie_dila=authentifie, **kwargs)

    def test_selection_archive_globale_jorfsimple(self):
        index = ('<a href="Freemium_jorf_simple_20261101-140000.tar.gz">ancien</a>'
                 '<a href="Freemium_jorf_simple_20261108-140000.tar.gz">récent</a>'
                 '<a href="JORFSIMPLE_20261109-000000.tar.gz">journal</a>')
        x = v.archive_globale_jorf_simple(index)
        self.assertEqual(x, v.SOURCE_JORFSIMPLE+'Freemium_jorf_simple_20261108-140000.tar.gz')

    def test_index_avec_fichier_truque_ignore(self):
        self.assertIsNone(v.archive_globale_jorf_simple('<a href="evil_20261031.tar.gz">z</a>'))

    def test_qualification_article_extension_expresse(self):
        self.assertTrue(v.confirmation_extension_obligatoire(self.corps, '2026-07-10'))

    def test_ne_confond_pas_visa_et_dispositif(self):
        t = ('Arrêté portant extension Hauts-de-France ouvriers bâtiment 1596 1597. '
             'Vu l’accord régional du 10 juillet 2026 relatif aux indemnités de petits déplacements. '
             'Article 1 Sont rendues obligatoires les stipulations de l’accord régional '
             'du 10 juillet 2026 relatif aux salaires.')
        self.assertFalse(v.confirmation_extension_obligatoire(t, '2026-07-10'))

    def test_qualification_document_date_et_sha(self):
        q = v.qualification_temoin_document(self.piece(), 'JO/JORFTEXT000055037883.xml','2026-10-31')
        self.assertEqual(q['date_publication_xml'],'2026-10-31')
        self.assertTrue(q['accord_temoignage_rendu_obligatoire_dans_article'])
        self.assertEqual(q['identifiant_jorf_detecte'],'JORFTEXT000055037883')
        self.assertEqual(len(q['empreinte_sha256_xml']),64)
        self.assertEqual(q['nor_detecte'], 'TRST2627911A')

    def test_date_incorrecte_exclue_meme_si_texte_identique(self):
        self.assertIsNone(v.qualification_temoin_document(self.piece(date_pub='2026-11-01'), 'jo.xml','2026-10-31'))

    def test_date_texte_sans_metadata_exclue(self):
        p = self.piece().replace(b'<DATE_PUBLI>2026-10-31</DATE_PUBLI>',b'<PUBLICATION>2026-10-31</PUBLICATION>')
        self.assertIsNone(v.qualification_temoin_document(p,'jo.xml','2026-10-31'))

    def test_flux_synthetique_trouve_temoignage_dans_article(self):
        dossier, bytes_zip = self.archive()
        try:
            a = self.analyse_bytes(bytes_zip, authentifie=True)
            self.assertTrue(a['preuve_officielle_retrouvee'])
            self.assertEqual(a['fichiers_xml_parcourus'],1)
            self.assertTrue(a['arret_apres_preuve'])
            self.assertEqual(v.verdict_temoin_v20(a)[0],'preuve_officielle_dila_retrouvee')
        finally:
            dossier.cleanup()

    def test_flux_non_officiel_ne_vaut_pas_preuve(self):
        dossier, bytes_zip = self.archive()
        try:
            a = self.analyse_bytes(bytes_zip, authentifie=False)
            self.assertFalse(a['preuve_officielle_retrouvee'])
            self.assertEqual(v.verdict_temoin_v20(a)[0],'test_local_non_authentifie')
        finally:
            dossier.cleanup()

    def test_test_local_cli_statut_distinct_et_code_non_nul(self):
        dossier, brut = self.archive()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'jo.tar.gz'; f.write_bytes(brut)
            rapport = Path(d)/'rapport.json'
            etat = Path(d)/'suivi'/'etat_veille.json'
            etat.parent.mkdir(parents=True)
            etat.write_text('{"suivi": "non modifié"}', encoding='utf-8')
            avant = etat.read_bytes()
            code = v.execution(['--mode','temoin','--archive-jorfsimple-locale',str(f),
                    '--rapport',str(rapport),'--etat',str(etat),
                    '--dossier-preuves',str(Path(d)/'preuves')])
            self.assertEqual(code,2)
            resultat = json.loads(rapport.read_text())
            self.assertEqual(resultat['etat_controle'],'test_local_non_authentifie')
            self.assertFalse(resultat['baremes_modifies'])
            self.assertEqual(etat.read_bytes(), avant)
            self.assertTrue((Path(d)/'preuves'/'temoin_jorf_2026-10-31.xml').is_file())
        dossier.cleanup()

    def test_source_officielle_simulee_dans_cli(self):
        dossier, contenu = self.archive()
        with tempfile.TemporaryDirectory() as d:
            rapport = Path(d)/'rapport.json'
            with patch.object(v, 'lire_index', return_value='<a href="Freemium_jorf_simple_20261108-140000.tar.gz">x</a>'), \
                 patch.object(v, 'ouvrir', side_effect=lambda _: io.BytesIO(contenu)):
                code = v.execution(['--mode','temoin','--rapport',str(rapport),
                         '--dossier-preuves',str(Path(d)/'preuves')])
            self.assertEqual(code,0)
            r = json.loads(rapport.read_text())
            self.assertEqual(r['etat_controle'],'preuve_officielle_dila_retrouvee')
            self.assertIn(v.SOURCE_JORFSIMPLE, r['temoin_jorf']['archive_officielle'])
        dossier.cleanup()

    def test_flux_incomplet_ne_vaut_pas_une_absence(self):
        dossier, contenu = self.archive()
        try:
            a = self.analyse_bytes(contenu[:40], authentifie=True)
            self.assertEqual(v.verdict_temoin_v20(a)[0],'controle_incomplet')
            self.assertTrue(a['erreur'])
        finally:
            dossier.cleanup()

    def test_archive_complete_sans_temoin_declaree_non_retrouvee(self):
        dossier, contenu = self.archive({'x.xml': self.piece(date_pub='2026-10-30')})
        try:
            a=self.analyse_bytes(contenu, authentifie=True)
            self.assertEqual(v.verdict_temoin_v20(a)[0], 'temoin_non_retrouve')
            self.assertTrue(a['archive_parcourue_en_entier'])
        finally:
            dossier.cleanup()

    def test_rejet_visa_seul_pas_de_fausse_preuve(self):
        texte = '''Arrêté portant extension d’accords régionaux Hauts-de-France des ouvriers du bâtiment.
        Vu l'accord régional du 10 juillet 2026 relatif aux indemnités de petits déplacements.
        Article 1 Sont rendues obligatoires les stipulations de l'accord du 10 juillet 2026 relatif aux salaires.'''
        dossier, contenu = self.archive({'JO/JORFTEXT000055037883.xml': self.piece(texte)})
        try:
            a=self.analyse_bytes(contenu, authentifie=True)
            self.assertEqual(v.verdict_temoin_v20(a)[0],'temoin_non_retrouve')
        finally:
            dossier.cleanup()

    def test_identifiant_différent_defaut_de_concordance(self):
        id2='JORFTEXT000055037884'
        dossier, contenu = self.archive({'JO/'+id2+'.xml':self.piece(ident=id2)})
        try:
            a=self.analyse_bytes(contenu, authentifie=True)
            self.assertEqual(v.verdict_temoin_v20(a)[0],'discordance_a_verifier')
            self.assertFalse(a['concordance_identifiant_attendu'])
        finally:
            dossier.cleanup()

    def test_recherche_ne_depend_pas_de_lidentifiant_jorf_prevu(self):
        q=v.qualification_temoin_document(self.piece(ident='JORFTEXT000055099999'), 'autre.xml','2026-10-31')
        self.assertIsNotNone(q)
        self.assertEqual(q['identifiant_jorf_detecte'],'JORFTEXT000055099999')

    def test_plusieurs_jours_meme_archive(self):
        dossier, contenu = self.archive({'un.xml': self.piece(date_pub='2026-10-30'),
                                          'deux.xml': self.piece()})
        try:
            a=self.analyse_bytes(contenu, authentifie=True)
            self.assertEqual(a['fichiers_xml_parcourus'],2)
            self.assertEqual(a['fichiers_du_jour_examines'],1)
            self.assertTrue(a['preuve_officielle_retrouvee'])
        finally:
            dossier.cleanup()

    def test_preuves_brutes_hash_correspond(self):
        import hashlib
        dossier, contenu = self.archive()
        try:
            with tempfile.TemporaryDirectory() as d:
                a=self.analyse_bytes(contenu, authentifie=True, dossier_preuves=d)
                doc=a['documents_candidates_extension'][0]
                fichier=Path(doc['piece_xml_conservee'])
                self.assertEqual(hashlib.sha256(fichier.read_bytes()).hexdigest(),doc['empreinte_sha256_xml'])
        finally:
            dossier.cleanup()

    def test_rapport_ecrit_meme_en_cas_de_date_invalide(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'rapport.json'
            code=v.execution(['--mode','temoin','--date-temoin','erreur','--rapport',str(f)])
            self.assertEqual(code,1)
            self.assertEqual(json.loads(f.read_text())['etat_controle'],'controle_incomplet')

    def test_aucun_lien_stock_global_produit_une_erreur_explicite(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'rapport.json'
            with patch.object(v,'lire_index',return_value='<html>aucun stock</html>'):
                self.assertEqual(v.execution(['--mode','temoin','--rapport',str(f)]),1)
            self.assertTrue(json.loads(f.read_text())['erreurs'])

    def test_limite_compression_refuse_flux_abusif(self):
        dossier, contenu=self.archive()
        try:
            with patch.object(v,'MAX_ARCHIVE_HISTORIQUE_COMPRIMEE', 12):
                a=self.analyse_bytes(contenu, authentifie=True)
            self.assertEqual(v.verdict_temoin_v20(a)[0],'controle_incomplet')
        finally:
            dossier.cleanup()

    def test_aucun_fichier_est_extrait_sur_disque_depuis_un_chemin_tar(self):
        dossier, contenu=self.archive({'../../../autre_dossier/JORFTEXT000055037883.xml':self.piece()})
        with tempfile.TemporaryDirectory() as d:
            try:
                a=self.analyse_bytes(contenu, authentifie=True, dossier_preuves=d)
                self.assertTrue(a['preuve_officielle_retrouvee'])
                self.assertEqual([x.name for x in Path(d).iterdir()],['temoin_jorf_2026-10-31.xml'])
            finally:
                dossier.cleanup()

    def test_arret_apres_preuve_nimplique_pas_exhaustivite(self):
        dossier, contenu = self.archive({'premier.xml':self.piece(), 'deuxieme.xml':self.piece()})
        try:
            a=self.analyse_bytes(contenu, authentifie=True)
            self.assertTrue(a['arret_apres_preuve'])
            self.assertFalse(a['archive_parcourue_en_entier'])
        finally:
            dossier.cleanup()



if __name__ == '__main__':
    unittest.main()
